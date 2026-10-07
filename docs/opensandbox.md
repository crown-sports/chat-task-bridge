# CSV task execution and OpenSandbox

The local executor runs one trusted, deterministic handler from this package. It does
not execute uploaded code, shell snippets, or generated code. The optional remote
executor runs the same handler in one newly created OpenSandbox per job. Local mode
is intended for the bundled CSV task, not as an isolation boundary.

## Use the file task

Upload CSV files, then use the bridge's merge command:

```text
/merge
/merge key=order_id
/merge key=order_id group=month sum=amount
```

Quote column names containing spaces, for example `key="Order ID"`.

The handler produces `merged.csv`, `summary.json`, and `report.html`. The report
contains counts, a 20-row preview, and a grouped-total chart when `group` and `sum`
are provided. The two `examples/orders-*.csv` fixtures produce six input rows, five
retained rows, one duplicate, and totals of `49.40` for October and `65.50` for
November. No model API or API key is needed for this computation.

| Rule | Behavior |
| --- | --- |
| Encoding | UTF-8, optionally with a BOM; comma-delimited CSV only |
| Schema | Same exact column names, reordered if needed; duplicate/blank headers or mismatches fail |
| Default deduplication | Stable exact-row deduplication |
| Explicit key | Identical keyed records collapse; conflicting records or blank keys fail |
| Grouped totals | Decimal arithmetic after deduplication; 24 integer and 12 fractional digits per value |
| Limits | 20 input files, 8 MiB each, 32 MiB total, 50,000 data rows, 128 columns, 1,000,000 cells |
| Output limits | 8 MiB per artifact and 32 MiB total; oversized results fail before publication |

Values are not silently trimmed or rewritten before matching. Numeric identifiers
remain strings. Grouped totals reject empty, nonnumeric, scientific notation, NaN,
and infinity values instead of substituting zero. Hash-based deduplication takes
expected linear time in the number of cells; this is a standard algorithm, not a
new research claim.

CSV output prefixes a single quote to cells whose first non-whitespace character
is `=`, `+`, `-`, or `@`, and to cells beginning with tab, CR, or LF. The checked
whitespace set is space, tab, CR, LF, vertical tab, form feed, and BOM. Headers are
checked too. This intentionally includes negative numbers in the exported CSV;
the original values remain available for decimal aggregation. The summary reports
the number of modified cells. Spreadsheet importers differ, so this is a documented
mitigation rather than a universal spreadsheet-execution guarantee. HTML content
is escaped, uses no JavaScript or remote assets, and applies a restrictive CSP.

## SDK adapter

Install the pinned optional dependency:

```sh
python -m pip install '.[sandbox]'
```

The tested SDK API surface is `opensandbox==1.1.0`. The host needs a running
OpenSandbox service configured for egress policy support and an image containing
Python 3.11 or newer. A service/image live compatibility run is still required;
the current tests use mocks and do not establish a supported server version.

```python
import os

from chatbridge.executor import OfficialSandboxFactory, OpenSandboxTableExecutor

executor = OpenSandboxTableExecutor(
    OfficialSandboxFactory(
        domain=os.environ["OPENSANDBOX_DOMAIN"],
        api_key=os.environ["OPENSANDBOX_API_KEY"],
        image=os.environ.get("OPENSANDBOX_IMAGE", "python:3.12-slim"),
        protocol=os.environ.get("OPENSANDBOX_PROTOCOL", "https"),
    ),
    timeout_seconds=90,
)
result = await executor.run(job_id, inbound_message)
```

After the administrator supplies these environment variables and the chosen
channel's credentials and sender allowlist, start the same executor through the CLI:

```sh
chatbridge run feishu --executor opensandbox --state .chatbridge
chatbridge run weixin --executor opensandbox --state .chatbridge
```

Run one worker per state directory. Choose either command; separate simultaneous
channel workers need separate state directories.

The default protocol is HTTPS. Use `protocol="http"` only for an explicitly
configured trusted local endpoint. Pin an image digest when deploying; the default
tag is convenient for development but mutable.

The adapter requests 1 CPU, 512 MiB memory, deny-all outbound network, a 180-second
sandbox lifetime, and server-proxied file/command access. It supplies no host mounts
or host environment variables. The API key stays in the host SDK connection
configuration; it is not embedded in uploaded files, command text, environment
variables, or task metadata. Credentials are excluded from object representations.

Attachment names are display metadata only. Script/input/result paths use a fresh
random identifier and never interpolate platform filenames or message text into a
shell command. The uploaded worker uses only Python's standard library. Downloaded
result JSON is bounded to 48 MiB, then validated against the fixed output manifest
and artifact limits.

A result is eligible for publication only if the SDK returns a completion event,
zero exit code, and no execution error. An empty/partial stream, missing exit code,
transport failure, or timeout becomes `ExecutionUncertain`; terminal failures are
separate. Cleanup is attempted in `finally` with a 15-second deadline. If creation
or cleanup cannot be confirmed, the server lifetime is a backstop, not evidence
that destruction has already occurred. Task retries require an explicit decision
through the bridge; this executor does not blindly resend commands.

## Verification scope

Tests cover real CSV parsing/aggregation, hostile filenames and HTML/formula data,
key conflicts, schema rejection, local/worker result parity, and injected remote
completion, timeout, transport, and cleanup failures. Optional SDK tests instantiate
the real pinned SDK models while mocking the server boundary. They do not connect
to a real OpenSandbox server or exercise Docker/Kubernetes isolation.

Primary references used for the adapter contract:

- [Published OpenSandbox Python package](https://pypi.org/project/opensandbox/1.1.0/)
- [Official Python SDK documentation](https://github.com/opensandbox-group/OpenSandbox/blob/main/sdks/sandbox/python/README.md)
- [Execution result model](https://github.com/opensandbox-group/OpenSandbox/blob/main/sdks/sandbox/python/src/opensandbox/models/execd.py)

The implementation is original code using the public SDK API. The upstream SDK is
an external Apache-2.0 dependency; its implementation is not copied into this project.
