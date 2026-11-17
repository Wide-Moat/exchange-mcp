# Working in exchange-mcp

Read MANIFESTO.md before changing anything; docs/code-review.md lists what blocks a merge;
docs/requirements.md lists every tool and every refusal with its test.

## Things that break silently

- **Logs carry content unless stopped.** httpx logs request URLs at INFO and the MCP SDK logs
  `str()` of a tool error. Errors carry a key; the sentence for the model is built separately.
  A full-flow test asserts that no password, token, address or subject reaches a log.
- **EWS errors arrive as HTTP 200.** A response is a failure when any `ResponseMessage` has
  `ResponseClass` other than `Success`; `ews/` turns that into an `EwsError`. Never read a body
  without going through it.
- **`CalendarView` does not page.** Large windows are split by time, not by offset.
- **A guard held by two checks is invisible to a one-line mutation.** `scripts/mutation_check.py`
  removes each safety rule and requires a test to fail; when a rule is held twice, mutate both.
- **`docs/tools.md` is generated** from the code.

## Process

Test first; every refusal has a test asserting `isError`, the text, and `ews.writes() == []`.
No host, domain, address or person of a real installation in any tracked file. English in the
repository. Pull requests open as drafts. Run `ocr review` before every push.
