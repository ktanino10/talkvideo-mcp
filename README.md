# TalkVideo MCP

An independently maintained toolkit for agent-driven narration and local video production.

> **Private personal media use; official speech is optional and disabled by default.**
> The official CoeFont API adapter is verified offline, not with live credentials or a
> particular person's voice. An eligible API contract, authorized voice, paid-use
> confirmation and private operator configuration are still required.
> Real-person lip-sync is not implemented. Diagnostic/mock media is **not speech or
> perceptual quality evidence**. The code repository is public; generated media stays private.

## Support

Questions, bugs, and feature requests for this project belong in [this repository's Issues](https://github.com/ktanino10/talkvideo-mcp/issues). Development and maintenance are the responsibility of this repository's maintainer.

This project is not an official TarakoTalk, CoeFont, or Hiroyuki project, and is not provided, reviewed, or endorsed by their authors or operators. Do not send support requests for this project to them.

The implementation is original and independently maintained, **not a fork**.
MIT covers the original code and skill ([LICENSE](LICENSE)). Third-party
code, voices, likenesses, footage, artwork, models and weights retain separate
terms. None of those rights are granted by this repository.

## What works

| Capability | This version |
| --- | --- |
| Agent-facing MCP | 11 structured, annotated tools over stdio; no HTTP listener |
| Text preparation | Read-only inline cues OR bounded UTF-8 files under an explicit input root |
| Long-text splitting | Grapheme-safe; prefers sentence/clause boundaries; preserves whitespace and numeric periods |
| Durable jobs | One sequential worker, bounded retries, cancellation, shutdown/restart and resume |
| Audio validation | Strict RIFF/WAVE, raw/normalized hashes, explicit local PCM conversion and frame timing |
| Revisions | New immutable revisions; reuse verified unchanged chunks; recompute downstream timing |
| Diagnostic audio/video | Explicit opt-in tone generation and local FFmpeg test-pattern mux |
| Official CoeFont speech | Implemented, offline-tested, unconfigured by default; operator authorization is separate |
| Real-person lip-sync | **Unavailable**; no model or footage integration is fabricated |
| Uploads / publication / cloud video | Not implemented; media remains local/private |

The official Python SDK is pinned to **`mcp==2.3.0`**, verified as a published,
non-yanked stable release on 2026-10-08. The server uses that SDK's low-level
`Server` API to control structured failures and avoid leaking input in SDK
validation logs. It does not use the old FastMCP decorator API or a private
HTTP bridge. Heavy model dependencies are not part of the package.

## Local setup and Copilot discovery

Requires Python 3.11+, `uv`, and macOS or Linux. The filesystem and process
boundary uses POSIX descriptor-relative I/O and process groups; Windows is
not supported by this release. FFmpeg and ffprobe are needed for diagnostic
video/full decoding and for explicitly enabled audio-format conversion.

From the **repository root**:

```sh
uv sync --locked
uv run --locked talkvideo-mcp capabilities
```

The shared Copilot CLI configuration is [`.github/mcp.json`](.github/mcp.json).
Start Copilot from this root and accept the normal folder-trust prompt only
after inspecting the repository. Use `/mcp show talkvideo` to inspect the
registered server. No trust-override environment variable is needed or
recommended. The default configuration leaves diagnostics disabled.

The skill is automatically discoverable at
[`.github/skills/talkvideo-creator/SKILL.md`](.github/skills/talkvideo-creator/SKILL.md).
A bare `skills/` folder and VS Code's `.vscode/mcp.json` are **not** this
Copilot CLI setup. Do not copy this file into a global skill directory as a
side effect of using the project.

Natural-language examples:

```text
TalkVideo向けの台本だけ整えて。数値と否定を変えず、音声はまだ作らないで。
ひろゆき風の解説動画を準備して。まず使える機能と台本を確認して。
この文章をひろゆき風に読み上げたい。利用許可と設定を確認してからにして。
TalkVideoの利用可能な機能を確認して、明示的な診断テストだけ進めて。
このrevisionのc0002の発音だけ直して。新しいプレビューを確認してから全文版へ。
```

Normal Zundamon/VOICEVOX requests stay with the user's existing `zundamon-video`
skill. This project does not edit it or substitute a new voice.

For another MCP client, launch this command as a child process from the repo
root; stdin/stdout are reserved for the protocol:

```sh
uv run --locked talkvideo-mcp serve --root output
```

For a deliberately selected synthetic diagnostic session, append
`--enable-diagnostics` to that local command. To override Copilot's shared
entry per checkout, the user may create a gitignored `.mcp.json` containing
the same `mcpServers` entry with that extra argument. No global MCP
configuration is changed by this repository.

`copilot mcp add NAME -- COMMAND [ARGS...]` is an optional **user action** that
writes user configuration, not an installation step this project runs.
See the [official Copilot CLI setup documentation](https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-mcp-servers).

## Production gates and review

Generated media is for **private personal use**. Check acquisition, modification
and private-use permissions for the actual source/voice/model. Publication
permission is not an additional gate for this private workflow; future
publication would require its own check. Personal use does not authorize
Maker automation, paid API usage, or a particular person's likeness.

**The Hiroyuki Maker route must not be automated under its published terms.**
The [Maker-specific terms](https://coefont.cloud/maker/terms), checked on
2026-10-08, prohibit automated use in Article 3(8). The personal
noncommercial/non-profit allowance in Article 2(4) remains subject to the
prohibitions, including abnormal server load (Article 3(6)) and infringement
of portrait, privacy and intellectual-property rights (Article 3(2)).
Public availability, a review flag, or older/generic CoeFont terms do not
override these Maker-specific restrictions.

The optional official REST API is a **separate, operator-authorized integration**,
not a way to automate Maker or override its terms. Voice/likeness/private media
permissions, model licensing and platform compatibility remain separate.
Clip-reuse permission does not imply permission for newly synthesized statements.
Wav2Lip's legacy dependencies and
separate restrictive terms, and MuseTalk's documented CUDA-oriented path,
are not evidence of usable Apple Silicon/MPS support. This release neither
downloads nor executes those models.

Both diagnostic and separately authorized speech workflows keep these review boundaries:

1. Draft and review the script. `prepare_script` changes nothing on disk.
   `save_revision` checks its digest and persists metadata, not audio.
2. The user acknowledges the exact stored script in the local terminal.
   Start a short audio preview; optionally make its diagnostic video preview.
3. Inspect the WAV/MP4, then the user acknowledges the actual preview.
   Full audio needs script + audio-preview acknowledgment; full video also
   needs video-preview acknowledgment.

The user runs, with the same configured output root:

```sh
uv run --locked talkvideo-mcp review VIDEO_NAME REVISION_ID --stage script --root output
uv run --locked talkvideo-mcp review VIDEO_NAME REVISION_ID --stage audio_preview --root output
uv run --locked talkvideo-mcp review VIDEO_NAME REVISION_ID --stage video_preview --root output
```

Each command requires a TTY and typing a digest-specific phrase. There is no
MCP approval tool or `--yes` option. Review receipts cannot configure or authorize
the official provider. Agents must not
write receipts or simulate the user's interaction. Receipts acknowledge local
review; they are **not cryptographic proof of a person's consent or media
rights**. Local files and the local operator remain a trust boundary.

## Optional official CoeFont API (not activated here)

The [official API 2.0.3 documentation](https://docs.coefont.cloud/en/) specifies
`POST https://api.coefont.cloud/v2/text2speech`, a 1..1000-character text body,
and a 302 redirect to the generated binary with a seven-day URL lifetime.
The adapter signs **UTC UNIX seconds concatenated with the exact JSON UTF-8
bytes sent** using HMAC-SHA256. The authenticated origin/path is fixed.
The initial integration requests WAV only. The typed adapter also validates
paired Japanese `yomi`/`accent` and documented prosody bounds; the MCP workflow
uses approved `spoken_text`, with voice/prosody selected by the operator.
For a short official preview, prepare a first chunk of at most 80 codepoints
(for example `limits.codepoints: 80`). The preview submits only that one
complete chunk, and rejects audio longer than 30 seconds rather than
silently treating a long render as a short preview.

This is **not a free-service claim**. Check the [current plans](https://coefont.cloud/selectPlan)
and your actual API contract, quota and voice permission. An existing account,
personal use, a sample UUID or documentation sample credentials establish none
of those. No account, subscription, key or live request is created by setup.

Only after separate authorization, an operator may copy
[`examples/official-api.toml.example`](examples/official-api.toml.example) into
gitignored `local/official-api.toml` and supply their own authorized voice UUID,
contract/private-use references, paid-request confirmation and exact trusted
download hosts. The example is disabled and contains no keys or real voice UUID.
Then launch the server with `--config local/official-api.toml`.

Credentials belong only in the server process environment:
`TALKVIDEO_COEFONT_ACCESS_KEY` and `TALKVIDEO_COEFONT_ACCESS_SECRET`.
The host must explicitly pass them through an approved secret-provisioning
mechanism; not every host inherits arbitrary parent-shell variables.
Do not put values in tool inputs, CLI arguments, TOML, fixtures, source or logs.
Without an explicit enabled and complete operator config, credentials are not
looked up. The project does not change host/global MCP settings.

Capability output distinguishes `implemented`, `configured`, `available`,
`authorization_status`, `execution_mode` and `live_verified`. Operator references
are attestations, not independently verified rights. Mock transports never
count as live evidence; half-mock/half-live transport pairs are rejected.
When actually authorized and enabled later, speech text leaves the machine for
the official API and may incur charges. Video processing remains local.

Downloads are deliberately stricter than an automatic redirect:

- No auth, signature, cookies or proxy credentials are forwarded to GETs.
- Every redirect needs HTTPS, no userinfo/fragment/custom port, and an exact
  operator-confirmed host. No wildcard or implicit suffix trust is supported.
  Public docs do not establish a trusted audio-download hostname; the default
  allowlist is empty, not a guessed storage bucket.
- DNS results must all be public addresses. Connections use the checked IP,
  while normal TLS certificate/SNI checks use the original hostname, avoiding
  a second DNS-resolution/rebinding gap.
- POST ambiguity is durable and never automatically resubmitted. A confirmed
  redirect or cached raw file resumes with **GET/local processing only**.
  Explicit API rejections remain journaled and require operator reconciliation.
- GET retries are cumulative and bounded. Delta-seconds and HTTP-date
  `Retry-After` deadlines survive cancel/restart/resume. A wait beyond the
  local automatic-wait budget returns `deferred` with `retry_not_before`,
  rather than shortening the wait or creating another generation.

Validated raw WAVs are retained before optional normalization. Mismatched
formats fail unless the operator explicitly enables conversion; supported
PCM/float WAV inputs are normalized locally to 16 kHz mono PCM16 with bounded
FFmpeg, or losslessly rewrapped when only the WAV header differs. Raw and
normalized hashes, formats, frames and the conversion method are recorded.
Unknown/invalid/partial audio is rejected, never renamed as valid PCM.

The private `.state/coefont/` journal/cache contains short-lived signed URLs
until a validated raw copy is durable, then removes them. Treat this state as
sensitive. No tool returns those URLs or API credentials. Cache identity includes
voice/options/contract and mock/live mode; an offline fixture cannot become
live speech by changing configuration.

## Tool contract

Names below have the `talkvideo_` prefix in the registered server.
`tools/list` is the authoritative input/output schema.

| Tool | Effect |
| --- | --- |
| `get_capabilities` | Read limits, gates, diagnostics and model notes |
| `prepare_script` | Read-only inline or rooted UTF-8 file preparation |
| `save_revision` | Persist a new immutable script/settings revision |
| `get_revision` | Read both tracks, stable cue IDs and provenance |
| `start_audio_job` | Return a durable preview/full audio job immediately |
| `start_video_job` | Local diagnostic test-pattern mux, not lip-sync |
| `get_job` | Read progress, failure classification and next action |
| `cancel_job` | Cancel work, retain completed chunks, reap subprocesses |
| `resume_job` | Verify hashes/settings/reviews; continue the same job |
| `inspect_output` | Raw/PCM hashes, frame timing, full video decode; page artifacts |
| `revise_cues` | New revision for targeted edits, never edit a resumed job |

Tool results include `ok`, typed `data` or `error`, `next_action`, and
`needs_user_action`. Failures also set MCP `isError=true`. A successful start
means a job was created, **not** that media exists or is complete. Jobs that
need review return `awaiting_review`, not fabricated media.

Example read-only preparation:

```json
{
  "cues": [
    {
      "cue_id": "intro",
      "display_text": "This is a diagnostic example.",
      "spoken_text": "This is a diagnostic example."
    }
  ],
  "normalization": "none",
  "limits": {"graphemes": 240, "codepoints": 1000, "utf8_bytes": 4000}
}
```

Normalization defaults to `none`. NFC is explicit and reported. Whitespace,
newlines, punctuation, emoji and combining sequences are not stripped. Chunks
rejoin each spoken cue exactly; prepared cues rejoin both complete tracks.
One over-limit grapheme is rejected, not split or silently dropped.

For a designated script file, configure `--input-root inputs` and call:

```json
{"script_file": "my-script.txt", "normalization": "none"}
```

`cues` and `script_file` are mutually exclusive. Paths are relative to the
explicit input root; traversal, symlinks, special files, invalid UTF-8 and
oversized files are rejected without echoing their contents. The input and
output roots must be disjoint so private provider state is not exposed as
script input. Newlines and a nonempty file's UTF-8 BOM are preserved, not
silently stripped. Preparation returns a source-file SHA-256 and size.
Saving a revision rereads the file and checks the prepared text digest.
Subsequent jobs use the immutable saved text, not a later edit to the input file.

## Bounded resources, recovery and artifacts

These are **host limits**, not verified permissions/limits of a provider:

| Resource | Limit |
| --- | --- |
| Each complete display/spoken track | 20,000 codepoints / 80,000 UTF-8 bytes |
| Cues / chunks per script | 128 / 512 |
| Per chunk | Configurable up to 1,000 graphemes, 1,000 codepoints, 4,000 bytes |
| Preview | Diagnostic: up to 3 chunks. Official: first chunk <=80 codepoints, actual audio <=30 seconds |
| Audio including inter-chunk gaps | 180 seconds, 16 kHz mono PCM16 |
| File / manifest | 64 MiB / 1 MiB |
| Concurrency / queue | One job / at most 8 queued or running jobs |
| Attempts per chunk | 3 total across all resumes; only guaranteed pre-submission failures retry |
| Provider attempt / job | 10 / 180 seconds |
| Official POST / individual GET-chain deadline | 30 / 30 seconds, plus bounded cleanup |
| Official requests cached per root / GET attempts per request | 512 / 3, without reset on resume |
| Download redirects / automatic cooldown waiting per call | 3 / at most 2 seconds; longer waits defer |
| Retained revisions / jobs per root | 200 / 1,000; archive manually or use a new root |

The diagnostic attempt limit is not an API authorization. The official API's
documented 1..1000-character bound is tested separately from host grapheme/byte
limits. Actual account quotas/eligibility remain unverified here. Diagnostic
tone duration does not estimate spoken duration.

Outputs live under the configured root, normally:

```text
output/
  .state/                     # Durable job records and single-writer lock
  video-name/
    r-<revision-id>/
      revision.json           # Script/settings and provenance
      artifacts.json          # Hashes, source links and frame-based timelines
      reviews/                # Local review acknowledgments
      chunks/                 # Validated immutable chunk WAVs
      raw/                    # Authorized API source WAVs, when applicable
      preview.wav
      narration.wav
      preview.mp4             # Optional diagnostic test pattern
      result.mp4              # Optional diagnostic test pattern
```

Text or settings changes belong in a **new revision**. Cue-specific edits
copy only verified unchanged chunk bytes and any raw-source provenance. Display-only edits retain speech
chunks. All assemblies, videos, timing and approvals are recomputed or
re-earned. Existing files are never overwritten by generation.

Resume verifies settings and SHA-256, including the assembled PCM and source
frame offsets. `ambiguous_submission` never automatically repeats a possibly
accepted request. `retry_exhausted` cannot be reset through resume.
`untracked_output` preserves an output published just before a crash rather
than regenerating it. Official request journals also prevent repeats across
jobs/revisions for the same payload and authorization context. An unowned stored `running` state is reported as
ownership-unconfirmed, not assumed to be active in the new server.

One writer owns a root; review receipts are separately append-only. Managed
I/O rejects traversal, symlinks and special files and uses descriptor-relative
operations. Use a private, trusted local directory, not a shared writable
folder or a network filesystem. This is not a sandbox against an attacker
with control of the same OS user or the configured root.

## Validation and evaluation

See [CONTRIBUTING.md](CONTRIBUTING.md) for lint/type/build/test commands.
Tests include real SDK stdio discovery and calls, background jobs outliving a
tool response, native cancellation and server shutdown/restart during an
unfinished long fixture, exact PCM timing, invalid/partial/HTML audio, and
local FFmpeg/ffprobe. MP4 validation includes complete strict decoding,
decoded frame counts, continuous packet timestamps, nominal 25 fps clocks,
and the fragmented MP4's AAC priming interval. Container average rate is
reported separately; a readable header alone is insufficient. A corrupted
payload regression confirms that distinction.

Official API tests use two injected mock transports and runtime-only fake
keys, not sample/live credentials. They exercise signing, status mapping,
redirect isolation, DNS pinning, durable cooldowns, ambiguous POSTs,
download-only restart/resume and normalization provenance. Synthetic fixture
acknowledgments are explicitly mocked, not real user reviews.

Ten fixed, independent, multi-call, read-only cases are defined in
[`evals/readonly.xml`](evals/readonly.xml):

```sh
uv run --locked python -m evals.run_readonly --output output/evaluations/read-only.json
```

The runner creates synthetic fixtures **before** evaluation, then only uses
read-only registered MCP tools through the native SDK stdio client. It
compares golden answers and verifies the fixture file hashes are unchanged.
Choose a new report filename for another run; reports are not overwritten.

The skill's two representative paired with-skill/baseline cases live in
[its evals directory](.github/skills/talkvideo-creator/evals/evals.json).
They measure wording/routing and targeted-revision/review decisions using
mocked workflow context and registered tool schemas, **not perceptual media
quality**. Their static standard skill-creator viewer and run outputs belong
in gitignored `output/`, not the public repository.

An SDK stdio pass is not evidence that the user's installed Copilot host has
trusted/authorized the project. A file's duration, waveform/RMS or container
validation is not evidence of natural speech or accurate lip-sync. Human
review remains necessary. This installation has not activated or live-verified
official speech, and real-person video remains blocked by missing permitted
footage/model integration, not by a newly imposed publication requirement.
