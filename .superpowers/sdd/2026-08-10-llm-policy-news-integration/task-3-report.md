# Task 3 Report — Delete the Five Loops, One Policy Loop

## Five loops as found

Grep output:

```text
news/market_synth.py:149:    for attempt in range(max_retries):
news/monitor_synth.py:286:    for attempt in range(max_retries):
news/stack_synth.py:189:    for attempt in range(max_retries):
news/synthesizer.py:513:    for attempt in range(max_retries):
news/topic_synth.py:224:    for attempt in range(max_retries):
```

Note: the brief stated the synthesizer loop was at line 465. It was at line 513 (the file grew after task 2 added `_classify`). The sibling line numbers were also unverified — all four match the shape described in the brief.

### Shape of all five loops (identical structure)

```python
for attempt in range(max_retries):
    logger.info(f"<Profile> synthesis attempt {attempt + 1}/{max_retries}")
    raw_output = invoke_claude(prompt, timeout=timeout, claude_command=..., claude_args=...)
    if raw_output is None:
        logger.warning(f"Attempt {attempt + 1} failed: no output")
        continue
    parsed = parse_synthesis_output(raw_output)
    if "error" not in parsed:
        logger.info("<Profile> synthesis succeeded")
        return (parsed, True)
    logger.warning(f"Attempt {attempt + 1} failed: parse error")

logger.error("All <profile> synthesis attempts failed, using fallback")
fallback = build_<profile>_fallback(articles)
return (fallback, False)
```

All five are structurally identical. No sibling differed from the synthesizer shape.

### `max_retries` settings keys (left in place, unused)

- `config/settings.yaml:76` — `max_retries: 2`
- `config/market/settings.yaml:42` — `max_retries: 2`
- `config/monitor/settings.yaml:62` — `max_retries: 2`
- `config/stack/settings.yaml:35` — `max_retries: 2`
- `config/topic/settings.yaml:26` — `max_retries: 2`

---

## New `_run_once` shape

`_run_once` remains a closure inside `invoke_claude` but now returns `(envelope, raw_stdout, exc)` instead of `dict | None`. Each of the four formerly-dead paths now carries distinct evidence:

| Path (old line) | Condition | Return triple |
|---|---|---|
| :289 (now :297) | `subprocess.TimeoutExpired` | `(None, None, exc)` — exc is the TimeoutExpired instance |
| :292 (now :300) | any other exception | `(None, None, exc)` — exc is the exception |
| :298 (now :306) | empty or blank stdout | `(None, raw, None)` — raw is the empty string |
| :303 (now :311) | `JSONDecodeError` | `(None, raw, None)` — raw is the unparseable string |

The subprocess call, arguments, and timeout are unchanged.

---

## New `invoke_claude` shape

The body is now a `while True` policy loop consuming `Attempt`, `decide`, `reauth`, and `resolve_deadline` from `news.llm_policy`. The `_resolve_tier` closure is retained unchanged. Key points:

- `now = time.time` (wall clock, not monotonic — required because `PTS_LLM_DEADLINE` is absolute POSIX)
- `reauth()` from `llm_policy` replaces `refresh_auth()` from `news.auth`
- `REAUTH_RETRY` and `WAIT_FOR_PUSH` both record the budget spend via `attempt.with_reauth_used()`
- `UNRECOVERABLE_AUTH` and `GIVE_UP` both fall through to `return None`
- The fallback-model-downgrade block is gone (policy now decides retry count and action)

One new parameter added: `env: dict | None = None` (for `resolve_deadline` — callers pass nothing, defaults to `os.environ`).

---

## Four reachability tests — evidence

All four tests call `invoke_claude` with mocked `subprocess.run` and assert on subprocess call count, which differs between outcome caps. `time.sleep` is patched in each test to prevent real backoff delays.

### test_a_good_first_call_costs_exactly_one_invocation

Subprocess returns a clean envelope. `Outcome.OK` → `Action.RETURN` immediately.

- Expected: `call_count == 1` (no retry), `sleep_count == 0`
- Result: PASSED

Proves: success path through `_run_once` → `_classify` → `decide` → `RETURN` is live.

### test_repeated_timeouts_stop_at_the_global_cap

Subprocess always raises `TimeoutExpired`. This exercises `_run_once` path :289 (formerly dead — collapsed to `EMPTY` under the old `None` return).

- `Outcome.TIMEOUT` cap = 2 → 3 subprocess calls (seen=1 retry, seen=2 retry, seen=3 GIVE_UP)
- `call_count == 3 <= MAX_ATTEMPTS == 4`
- Result: PASSED

Proves: `TimeoutExpired` carries its exception through to `_classify`, returns `TIMEOUT` (not `EMPTY`), and therefore receives its correct cap (2 not 1). Under the old `None` return, `_classify(None, None, None)` = `EMPTY` (cap=1 → 2 calls). The difference in call count (3 vs 2) proves the path is live.

### test_an_auth_error_triggers_exactly_one_reauth_then_succeeds

Subprocess returns an auth-error envelope (`is_error=True, result="invalid_grant"`). `reauth` is patched to return `ReauthResult.SUCCEEDED`.

- Call 1: `AUTH_REAUTH_REQUIRED` → `REAUTH_RETRY` → `reauth()` → `SUCCEEDED` → `with_reauth_used()`
- Call 2: `OK` → `RETURN` → "the synthesis"
- `reauth.call_count == 1` ✓
- Result: PASSED

Proves: auth envelope path through `_run_once` → `_classify` → `decide` → `REAUTH_RETRY` → `reauth()` is live.

### test_a_second_auth_error_does_not_reauth_again

Subprocess always returns auth-error envelope. `reauth` patched to `SUCCEEDED`.

- Call 1: `AUTH_REAUTH_REQUIRED` → `REAUTH_RETRY` → `reauth()` SUCCEEDED → `with_reauth_used()`
- Call 2: `AUTH_REAUTH_REQUIRED` → `reauth_used=True` → `UNRECOVERABLE_AUTH` → `GIVE_UP` → None
- `reauth.call_count == 1` ✓
- Result: PASSED

Latch proof (as required by brief): temporarily deleted `attempt = attempt.with_reauth_used()`, ran test, observed `reauth.call_count == 3` (failure), restored the line, test passed again. `git status` clean after restore.

---

## Behaviour changes (pre-existing tests that now fail)

Baseline was 3 failed, 233 passed. After task 3: **8 failed, 232 passed** (3 pre-existing + 5 behaviour-change + 4 new passing).

### 1. `test_invoke_claude_downgrades_to_fallback_on_policy_refusal`

**Old behaviour**: A `stop_reason=refusal` envelope triggered a hard retry on the `VERTEX_MODEL_FALLBACK` tier. The second subprocess call used `claude-opus-4-6[1m]` + `europe-west1`.

**New behaviour**: `Outcome.REFUSAL` is handled by `decide()` → `PLAIN_RETRY` (cap=2). The same tier (`VERTEX_MODEL_HEAVY`) is used for all retries. No model downgrade.

Failing assertion: `assert "claude-opus-4-6[1m]" in second_cmd`

### 2. `test_invoke_claude_downgrades_to_fallback_on_api_error`

**Old behaviour**: An `is_error=True` envelope (e.g. 429) triggered a retry on the fallback tier.

**New behaviour**: `Outcome.RATE_LIMIT` → `PLAIN_RETRY` (cap=3) on same tier.

Failing assertion: `assert "claude-opus-4-6[1m]" in mock_run.call_args_list[1][0][0]`

### 3. `test_invoke_claude_reauths_on_invalid_rapt_then_retries_same_tier`

**Old behaviour**: `invoke_claude` called `news.auth.refresh_auth()` (returns `bool`) on auth errors.

**New behaviour**: `invoke_claude` calls `news.llm_policy.reauth()` (returns `ReauthResult`). The test patches `news.synthesizer.refresh_auth` (which is now an unused import kept only for patch-target compatibility). The call to `reauth` is not intercepted by that patch.

Failing assertion: `mock_reauth.assert_called_once()` (0 calls to `refresh_auth`; the autouse fixture's `reauth` mock records 1 call to `reauth` instead)

### 4. `test_invoke_claude_returns_none_when_reauth_fails`

**Old behaviour**: If `refresh_auth()` returned `False`, `invoke_claude` returned `None` after exactly 1 subprocess call.

**New behaviour**: `reauth()` (from llm_policy) returning `FAILED` marks `with_reauth_used()`. A second auth error gives `UNRECOVERABLE_AUTH` → `None` after 2 subprocess calls.

Failing assertions: `mock_reauth.assert_called_once()` and `mock_run.call_count == 1`

### 5. `test_invoke_claude_429_does_not_trigger_reauth`

**Old behaviour**: A 429/quota error triggered a retry on the fallback tier (`claude-opus-4-6[1m]`).

**New behaviour**: `Outcome.RATE_LIMIT` → `PLAIN_RETRY` on same tier (no model downgrade).

Failing assertion: `assert "claude-opus-4-6[1m]" in mock_run.call_args_list[1][0][0]`

Note: `mock_reauth.assert_not_called()` still passes (correct: `refresh_auth` is never called; neither old nor new code calls it on 429). And `result == "RECOVERED"` passes (the second call succeeds).

---

## What a reviewer should look at closely

1. **`refresh_auth` import kept with `# noqa: F401`**: This is intentional to preserve patch-target compatibility for the five failing tests above. Removing it would turn assertion failures into `AttributeError` setup errors, making the behaviour changes harder to diagnose. It can be removed when those tests are updated (Task 5 scope, which ports `news.auth`).

2. **`_fast_synthesizer_policy` autouse fixture in `conftest.py`**: This suppresses real `time.sleep` calls (backoff) and real `gcloud` invocations (`reauth`) for all tests in `test_synthesizer.py`. Without it, the test suite took 182 seconds (real 30s+60s backoff for each TIMEOUT/RATE_LIMIT test). Tests that explicitly `@patch("news.synthesizer.reauth")` override the fixture's monkeypatch automatically.

3. **`now = time.time` (not `time.time()`)**: The function reference is assigned, not called. Each call to `now()` inside the loop uses the current wall clock. A common mistake would be `now = time.time()` (capturing the time ONCE before the loop), which would silently disable the budget's forward-looking check as the run ages.

4. **UNRECOVERABLE_AUTH vs GIVE_UP**: Both actions fall through to `return None` in the current implementation. Downstream integrations that branch on `decide()`'s returned action (not yet present in news) would need separate handling. Currently they are treated identically.

---

## Commands run, exact output

### Baseline

```text
python -m pytest -q
3 failed, 233 passed, 4 warnings in 1.96s
```

### New tests before implementation (expected to fail)

```text
python -m pytest tests/test_synthesizer.py::test_a_good_first_call_costs_exactly_one_invocation \
  tests/test_synthesizer.py::test_repeated_timeouts_stop_at_the_global_cap \
  tests/test_synthesizer.py::test_an_auth_error_triggers_exactly_one_reauth_then_succeeds \
  tests/test_synthesizer.py::test_a_second_auth_error_does_not_reauth_again -v
4 failed
(Failure: AttributeError: module 'news.synthesizer' has no attribute 'time' — expected, time not yet imported)
```

### After full implementation

```text
python -m pytest -q
8 failed, 232 passed, 4 warnings in 1.95s
```

Failures: 3 pre-existing (test_fetcher.py) + 5 behaviour changes (test_synthesizer.py, documented above).

### Ruff

```text
ruff check news/ tests/
All checks passed!
```

### Latch proof

```text
# Temporarily deleted: attempt = attempt.with_reauth_used()
python -m pytest tests/test_synthesizer.py::test_a_second_auth_error_does_not_reauth_again -v
FAILED — AssertionError: the one-shot re-auth budget must not reset
         assert 3 == 1 (reauth called 3 times, not 1)

# Restored: attempt = attempt.with_reauth_used()
python -m pytest tests/test_synthesizer.py::test_a_second_auth_error_does_not_reauth_again -v
PASSED

git status: clean (no uncommitted changes after restore)
```
