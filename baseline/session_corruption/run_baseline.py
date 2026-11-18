"""One call to the original Agent, no external test feedback/retry loop.

Usage: python run_baseline.py CANDIDATE_REPO NEW_RESULT_DIRECTORY
The result directory must be outside the candidate repository.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import time
import traceback


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("results", type=Path)
    args = parser.parse_args()
    candidate, results = args.candidate.resolve(), args.results.resolve()
    if results == candidate or candidate in results.parents:
        parser.error("Results must be outside the candidate repository")
    if not (candidate / "corecoder/session.py").is_file():
        parser.error("Candidate does not contain corecoder/session.py")
    results.mkdir(parents=True, exist_ok=False)
    os.chdir(candidate)
    sys.path.insert(0, str(candidate))
    # Import the unchanged runtime before it edits candidate source files.
    from corecoder import Agent, Config
    from corecoder import checkpoints
    from corecoder.llm import LLM, LiteLLM
    from corecoder.permissions import Permission

    checkpoints.CHECKPOINTS_FILE = results / "checkpoints.json"
    checkpoints.clear()

    config = Config.from_env()
    if not config.api_key:
        parser.error("Configure your model API key in the environment first")
    cls = LiteLLM if config.provider == "litellm" else LLM
    llm = cls(model=config.model, api_key=config.api_key, base_url=config.base_url,
              temperature=config.temperature, max_tokens=config.max_tokens)
    agent = Agent(llm=llm, max_context_tokens=config.max_context_tokens,
                  permission=Permission(allow_all=True))
    prompt = Path(__file__).with_name("task.md").read_text(encoding="utf-8")
    (results / "task.md").write_text(prompt, encoding="utf-8")
    metadata = {"model": config.model, "provider": config.provider,
                "temperature": config.temperature, "max_output_tokens": config.max_tokens,
                "max_context_tokens": config.max_context_tokens, "max_rounds": agent.max_rounds,
                "candidate": str(candidate), "python": sys.version,
                "hooks": False, "mcp": False, "external_retry": False}
    started = time.monotonic()
    status = 0
    try:
        answer = agent.chat(prompt)
        (results / "answer.txt").write_text(answer or "", encoding="utf-8")
        metadata["execution"] = "returned"
    except (Exception, KeyboardInterrupt):
        metadata["execution"] = "error_or_interrupted"
        (results / "error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        status = 1
    finally:
        metadata.update(elapsed_seconds=round(time.monotonic() - started, 3),
                        prompt_tokens=llm.total_prompt_tokens,
                        completion_tokens=llm.total_completion_tokens,
                        estimated_cost_usd=llm.estimated_cost,
                        acceptance="not_run")
        (results / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        (results / "messages.json").write_text(json.dumps(agent.messages, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Execution recorded in {results}; run external acceptance separately.")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
