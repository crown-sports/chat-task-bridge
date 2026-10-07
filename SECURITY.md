# Security boundaries

- Channel credentials are supplied by the deployer. Weixin login credentials and reply context are stored in owner-only local state. State is private, **not encrypted at rest**; protect the host, backups and filesystem.
- `.env`, account state, SQLite databases, demo state and logs are excluded from Git. Do not publish a state directory or serve it as web content. Private files are written atomically; artifact writes sync the file and containing directory before ledger commit.
- Sender access is deny-by-default. The initial release handles one channel account per worker. File inboxes include channel, account, conversation, thread and sender; it does not implement a multi-tenant authentication service.
- HTTP failures are mapped to bounded categories. The CLI suppresses raw provider responses, HTTP wire logging and unhandled exception details. Applications embedding the library must avoid logging inputs, credentials, HTTP headers and signed media/QR URLs.
- Attachment storage uses a SHA-256 path independent of filenames. Reads verify integrity; uploads and downloads have limits. Expected file content and original reply context remain in the private ledger/artifact store until cleanup.
- The local executor runs only a packaged CSV function, never submitted code. The optional OpenSandbox executor requires a correctly secured OpenSandbox deployment; this package does not independently implement container isolation.
- CSV output prefixes potentially active spreadsheet formulas with a quote, including headers and negative numeric text. Original values are used for calculations. Do not remove escaping blindly when opening untrusted files in spreadsheet applications.
- Unknown execution or delivery outcomes are not automatically repeated. Retrying unknown delivery is an explicit operator action and may send duplicate messages.
- Storage limits apply per file/batch, not to total disk use. `gc` expires old completed jobs and staged files, removes unreferenced artifacts, retains ambiguous work and lightweight event tombstones. Run maintenance with the worker stopped.
- Normal crash recovery is tested offline. Platform retry guarantees, live account permissions, TLS deployment and real sandbox isolation remain deployment responsibilities pending live validation.

Do not put tokens or unredacted payloads in public issues. For a vulnerability report, first send a minimal redacted description through the repository's available private reporting mechanism; if none is configured, request a private contact without publishing exploit details or credentials.
