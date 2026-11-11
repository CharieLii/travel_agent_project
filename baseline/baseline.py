"""Single-call LangChain itinerary baseline; no tools or repair loop."""

import argparse
from contextlib import redirect_stdout
import io
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from time import perf_counter
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "dataset"
sys.path.insert(0, str(ROOT / "evaluation"))
sys.path.insert(0, str(ROOT / "scripts"))
from render_results import place_names, render_answer, render_row
import score_baseline

HK_TZ = ZoneInfo("Asia/Hong_Kong")
PROMPT_VERSION = "baseline-no-routes-v2"
MAX_OUTPUT_TOKENS = 1536


def is_openrouter():
    return (os.getenv("OPENAI_BASE_URL") or "").rstrip("/") == "https://openrouter.ai/api/v1"

INSTRUCTIONS = """你是香港旅行规划助手。根据用户的最新要求和提供的本地资料，用中文生成行程。

规则：
1. 目的地、日期、必游景点、住宿、同行人员、景点偏好类型和舒适度均为选填。信息缺失时优先提供暂定或部分行程，不编造用户需求。日期或天数未知时，按已有知识推荐行程天数和安排，不限定为一天；未知日期填 null，说明为暂定方案。
2. 仅使用资料库内的香港景点。库外景点或其他目的地应说明当前无法安排。活动也可包含资料库内的住宿地点。
3. 根据常规开放时间安排行程；开放时间未知时，不要编造。暂定游览时长不得说成官方建议。
4. 交通方式和路线安排按你已有知识建议 walk 或 transit。from_place_id 表示交通起点；同一天后续活动的起点应与上一站一致，未知起点填 null。
5. 用户要求与已知事实或其他要求冲突时，优先提供已合规的这部分行程，后文要求用户澄清冲突，并将问题加入 questions 列表；不要把已知不可行的活动放入正式行程。
6. 不宣称路线已核验或最优。
未提供路线数据，walking_minutes 和 public_transit_minutes 均填 null。

只输出一个 JSON 对象，不使用代码围栏：
{"status":"plan|clarification|out_of_scope|help","message":"简要说明",
 "days":[{"date":null,"activities":[{"place_id":"资料库 ID","start_time":"HH:MM 或 null",
 "end_time":"HH:MM 或 null","note":"安排说明","transport_from_previous":{
 "from_place_id":null,"recommended_mode":null,"walking_minutes":null,
 "public_transit_minutes":null}}]}],"assumptions":[],"questions":[]}
没有行程时 days 为 []；未知值使用 JSON 的 null。日期和时间已知时分别使用 YYYY-MM-DD 和 HH:MM 字符串，不要把“或 null”作为实际输出。"""


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def catalog_text():
    """Provide attraction and accommodation data without route information."""
    return json.dumps(
        {
            "attractions": read_json(DATA / "hong-kong-attractions.json"),
            "accommodations": read_json(DATA / "hong-kong-accommodations.json"),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def build_messages(history, catalog, reference_datetime):
    system = (
        f"{INSTRUCTIONS}\n香港时区；解释相对日期的参考时间：{reference_datetime}"
        f"\n本地资料：{catalog}"
    )
    return [("system", system)] + [
        (message["role"], message["content"]) for message in history
    ]


def build_model():
    if not os.getenv("OPENAI_API_KEY") or not os.getenv("TRAVEL_MODEL"):
        raise ValueError("请先在本机配置 OPENAI_API_KEY 和 TRAVEL_MODEL。")
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=os.environ["TRAVEL_MODEL"],
        base_url=os.getenv("OPENAI_BASE_URL") or None,
        temperature=0,
        max_tokens=MAX_OUTPUT_TOKENS,
        timeout=60,
        max_retries=0,
        use_responses_api=False,
        extra_body={"reasoning": {"enabled": False}} if is_openrouter() else None,
    )


def generate(model, messages):
    start = perf_counter()
    response = model.invoke(messages)
    # Keep the raw answer, including invalid JSON, for unbiased offline review.
    return {
        "output": response.content,
        "latency_seconds": round(perf_counter() - start, 3),
        "usage": response.usage_metadata,
        "response_model": response.response_metadata.get("model_name"),
        "finish_reason": response.response_metadata.get("finish_reason"),
    }


def batch(args, catalog):
    data = read_json(DATA / f"travel-dialogue-{args.split}.json")
    # Case labels stay in the evaluation harness and never enter generation.
    cases = [{"id": case["id"], "messages": case["messages"]}
             for case in data["cases"] if args.case is None or case["id"] == args.case]
    if not cases:
        raise ValueError(f"{args.split} 集找不到案例 {args.case}。")
    reference = data["reference_datetime"]
    if args.preview:
        for case in cases:
            print(json.dumps({
                "case_id": case["id"],
                "messages": build_messages(case["messages"], catalog, reference),
            }, ensure_ascii=False))
        return 0

    model = build_model()
    stamp = datetime.now(HK_TZ).strftime("%Y%m%d-%H%M%S-%f")
    names = place_names()
    if args.output:
        failures, scoring_failures = write_results(cases, model, catalog, reference, args, args.output, names)
    else:
        run_dir = ROOT / "results" / "baseline" / f"baseline-{args.split}-{stamp}"
        run_dir.mkdir(parents=True, exist_ok=False)
        failures = 0
        scoring_failures = 0
        case_scores = []
        for case in cases:
            case_dir = run_dir / case["id"]
            case_dir.mkdir(exist_ok=False)
            model_failures, score_failures = write_results(
                [case], model, catalog, reference, args, case_dir / "result.jsonl", names
            )
            failures += model_failures
            scoring_failures += score_failures
            score_file = case_dir / "result.score.json"
            try:
                saved_scores = read_json(score_file)
                case_scores.extend(saved_scores["cases"])
            except (OSError, ValueError, KeyError, TypeError):
                metric_names = ("输出格式通过", "生成行程", "目的地", "到离时间", "必游景点", "景点开放",
                         "指定住宿", "景点类型偏好", "舒适度偏好", "行程可行率", "顺路达标率", "景点重复率")
                case_scores.append({"case_id": case["id"], **{
                    name: {"数值": None, "原因": "评分失败或评分文件无法读取"} for name in metric_names}})
            # Replace the run summary after each case so completed scores survive interruption.
            temporary = run_dir / ".scores.json.tmp"
            temporary.write_text(json.dumps({"summary": score_baseline.summarize_scores(case_scores), "cases": case_scores},
                                           ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                                 encoding="utf-8")
            temporary.replace(run_dir / "scores.json")
        print(f"本次运行目录：{run_dir}")
        print(f"评分汇总：{run_dir / 'scores.json'}")
    if scoring_failures:
        print(f"评分失败 {scoring_failures} 个结果文件，原始输出已保留。", file=sys.stderr)
    return 1 if failures or scoring_failures else 0


def write_results(cases, model, catalog, reference, args, path, names):
    readable_path = path.with_suffix(".md")
    if readable_path == path:
        readable_path = path.with_name(path.name + ".view.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive create prevents accidentally replacing an earlier experiment.
    with readable_path.open("x", encoding="utf-8") as readable:
        try:
            stream = path.open("x", encoding="utf-8")
        except OSError:
            readable_path.unlink()
            raise
        readable.write("# Baseline 行程结果\n\n")
        with stream:
            failures = run_cases(cases, model, catalog, reference, args, stream, readable, names)
    print(f"结构化结果：{path}")
    print(f"可读行程：{readable_path}")
    score_path = path.with_name(path.stem + ".score.md")
    score_json_path = path.with_name(path.stem + ".score.json")
    try:
        output = io.StringIO()
        with redirect_stdout(output):
            score_data = score_baseline.main([str(path)])
        score_text = output.getvalue()
        with score_path.open("x", encoding="utf-8") as score_stream:
            score_stream.write("# Baseline 评分结果\n\n```text\n" + score_text + "```\n")
        score_json_text = json.dumps(score_data, ensure_ascii=False, indent=2, allow_nan=False)
        with score_json_path.open("x", encoding="utf-8") as score_stream:
            score_stream.write(score_json_text + "\n")
        print(f"评分结果：{score_path}")
        print(f"评分数据：{score_json_path}")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"评分失败，行程结果已保存：{exc}", file=sys.stderr)
        return failures, 1
    return failures, 0


def run_cases(cases, model, catalog, reference, args, stream, readable, names):
    failures = 0
    for case in cases:
        messages = build_messages(case["messages"], catalog, reference)
        result = {
            "case_id": case["id"], "split": args.split, "method": "llm_direct_no_routes",
            "model": os.environ["TRAVEL_MODEL"], "temperature": 0,
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "reasoning_enabled": False if is_openrouter() else None,
            "prompt_version": PROMPT_VERSION, "reference_datetime": reference,
            "messages": case["messages"],
        }
        try:
            result.update(generate(model, messages))
            result["status"] = "ok"
        except Exception as exc:
            # Avoid saving provider payloads, which may include private configuration.
            failures += 1
            result.update(status="error", error_type=type(exc).__name__,
                          http_status=getattr(exc, "status_code", None))
        stream.write(json.dumps(result, ensure_ascii=False) + "\n")
        stream.flush()
        readable.write(render_row(result, names) + "\n")
        readable.flush()
        if result["status"] == "error":
            detail = (f"HTTP {result['http_status']}" if result.get("http_status")
                      else result.get("error_type", "未知错误"))
            print(f"{case['id']} 模型请求失败：{detail}；详情见结果文件。", file=sys.stderr)
    return failures


def chat(catalog):
    model = build_model()
    reference = datetime.now(HK_TZ).isoformat()
    history = []
    names = place_names()
    print("旅行规划 baseline。输入 /reset 清空对话，/exit 退出。")
    while True:
        try:
            user_text = input("你：").strip()
        except (EOFError, KeyboardInterrupt):
            return 0
        if user_text == "/exit":
            return 0
        if user_text == "/reset":
            history.clear()
            reference = datetime.now(HK_TZ).isoformat()
        elif user_text:
            current = history + [{"role": "user", "content": user_text}]
            result = generate(model, build_messages(current, catalog, reference))
            print("助手：\n" + render_answer(result["output"], names))
            # On each turn, pass previous conversation without maintaining extra state.
            history = current + [{"role": "assistant", "content": result["output"]}]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("chat", help="交互式输入旅行需求")
    run = sub.add_parser("run", help="运行现有对话案例")
    run.add_argument("--split", choices=["dev", "test"], default="dev")
    run.add_argument("--case", help="只运行指定案例，如 DEV-01")
    run.add_argument("--preview", action="store_true", help="仅打印请求，不调用模型")
    run.add_argument("--output", type=Path, help="保存结果的 JSONL 路径（必须尚不存在）")
    args = parser.parse_args()
    try:
        catalog = catalog_text()
        return chat(catalog) if args.command == "chat" else batch(args, catalog)
    except (OSError, ValueError) as exc:
        print(f"运行失败：{exc}", file=sys.stderr)
    except ImportError:
        print("请先用 Python 3.10+ 执行 pip install -r requirements.txt。", file=sys.stderr)
    except Exception as exc:
        print(f"模型调用失败：{type(exc).__name__}；请检查模型名、密钥和 API 地址。", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
