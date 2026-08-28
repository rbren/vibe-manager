#!/usr/bin/env python3
"""Live agent-server checks for the automation's LLM profile handling.

These hit the real agent server rather than a stub: the bug they guard
against was a wrong assumption about the response shape, which only a real
response can catch. `/api/profiles` returns `model` at the top level of each
profile, while `/api/profiles/<name>` nests the whole LLM config under
"config" — mixing the two up silently yields a null model.

Requires a reachable agent server; skips (exit 0) when there is none.

Run: python3 tests/test_vibestore_agent.py
"""

import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "automation"))

import vibestore  # noqa: E402

failures = []


def check(label, actual, expected):
    if actual == expected:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}: expected {expected!r}, got {actual!r}")
        failures.append(label)


def check_true(label, value, detail=""):
    if value:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}{': ' + detail if detail else ''}")
        failures.append(label)


def main() -> int:
    try:
        listed = vibestore.llm_profiles()
    except Exception as exc:  # noqa: BLE001 - any transport failure means "no server"
        print(f"SKIP: agent server unreachable ({exc})")
        return 0
    if not listed["profiles"]:
        print("SKIP: agent server reports no LLM profiles")
        return 0

    print("llm_profiles()")
    for p in listed["profiles"]:
        check_true(f"profile {p['name']!r} has a model", bool(p["model"]),
                   "model is None — check whether the list endpoint nests it "
                   "under 'config'")
    check_true("an active profile is reported", bool(listed["active_profile"]))

    name = listed["active_profile"] or listed["profiles"][0]["name"]
    expected_model = next(p["model"] for p in listed["profiles"] if p["name"] == name)

    print("\nagent_settings_payload()")
    base = vibestore.agent_settings_payload()
    check("tools resolve to the default exec set", base["tools"], None)
    check_true("settings carry an llm", bool(base.get("llm")))

    scoped = vibestore.agent_settings_payload(name)
    check(f"profile {name!r} swaps in its own model",
          scoped["llm"]["model"], expected_model)
    check_true("usage_id is preserved", bool(scoped["llm"].get("usage_id")))

    print("\nunknown profile")
    try:
        vibestore.agent_settings_payload("definitely-not-a-real-profile")
        print("  FAIL unknown profile raises ValueError: did not raise")
        failures.append("unknown profile raises")
    except ValueError as exc:
        check_true("unknown profile raises ValueError", True)
        check_true("error lists the available profiles", name in str(exc), str(exc))
    except urllib.error.HTTPError as exc:
        print(f"  FAIL unknown profile raises ValueError: got HTTP {exc.code}")
        failures.append("unknown profile raises")

    print()
    if failures:
        print(f"FAILED ({len(failures)}): {', '.join(failures)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
