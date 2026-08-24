# Changelog

## Unreleased

- **Fix: a record's primary checksum was a verdict about a capture it never named.** A
  manifest record can own several delivered paths, and the primary `provenance` is whichever
  of them a sidecar matched first. For a bundle that is the bundle root, which appears in no
  `raw_paths` at all — so nothing on the record said which capture the flat `checksum` and
  `checksum_verified` fields described, while every *secondary* capture had named its own
  path since `additional_provenance` shipped.

  The record now carries `provenance.path`, and the two consumers that had to work around
  its absence stop doing so. A strict-mode refusal names the capture it is about whether that
  capture is the primary or a secondary one, where a primary mismatch previously named only
  the record — ambiguous for exactly the multi-capture records the mismatch arm exists to
  catch. And an exported citation carries `provenance_path`, so a consumer reading
  `checksum_verified` can say what was verified.

  Two consequences worth expecting. Manifest records grow a field, so a record rewritten by
  the next inventory run has a new fingerprint, on the same terms as the other field
  additions in this release. And a `--require-checksum` or `--reject-mismatch` refusal over a
  bundle record now reports a `path` where it reported none, which is additive to the
  envelope rather than a change to which records refuse.
- **Fix: deleting raw evidence an order was issued against was reported as a broken raw
  tree.** Both completed acquisition arms decide, from two snapshots, whether a raw file
  that existed when the order was issued has been changed or removed. That verdict needs
  nothing from inventory — and it was being held until after a re-derivation of the whole
  delivered tree, which can fail. When it failed, the raise travelled out ahead of the
  verdict already in hand: the operator was told "delivered raw evidence could not be
  re-derived by source inventory rules" and sent to repair a raw tree, and the file they
  had deleted was never named.

  The two are not independent, which is what makes the wrong answer the likely one rather
  than a coincidence. Deleting a raw file a manifest record references is a plausible reason
  the derivation cannot run at all, so the case that produces the misdirection is the
  ordinary one.

  The check now runs before the derivation, and the refusal names the file. One consequence
  for anyone reading refusals: an edit to raw evidence that existed at issuance now answers
  `changed or removed raw evidence that existed when the order was issued` rather than
  sharing a refusal with deliveries outside the fulfilled source scope. The shared wording
  described only the other half, so a sidecar edit was reported as a scope problem. The
  unexpected-new-paths refusal keeps its message and its details unchanged.

- **Change: inside a pending acquisition order, `fulfill` and `reopen` now file a claim the
  controller commits on acceptance, instead of writing durable state as they run.** An
  order is one of those when it carries `phase: acquisition`, whether the acquirer is a
  delegated host or the workspace's own providers, and nothing outside one is touched.
  `source_requests.py fulfill` writes a claim at
  `runs/order-claims/<orchestration_id>/<action_id>.json` and does not write
  `sources/source-requests.jsonl` at all, so the record it is about stays `status: "open"`
  with `source_id: null` for the duration of the order. `question_resolve.py reopen` files
  a claim the same way and leaves the question page `blocked`, with its `blocked_reason`
  and `blocking_request_ids` where they were. The controller commits the accepted claims
  during the wet verification pass, after every other check in it has passed, so a refused
  submission commits nothing. A claim asserts bookkeeping and never evidence — which source
  answered which request, which sources reopen a question — and every evidence check still
  runs against the manifest, the normalized tree and the raw tree exactly as it did. One
  new refusal comes with it: a request or question scoped by two live delegated acquisition
  orders at once is refused as ambiguous rather than attributed to whichever session sorted
  first, because guessing files the claim into one order's ledger and leaves the other
  submitting with nothing to show.

  Four things close with it, and they were four symptoms of one cause: the acquirer wrote
  durable state before the controller had looked at the evidence, and what is written
  outlives the order that wrote it. A refused submission no longer leaves a fulfilment
  standing, because there is nothing written to leave. An `outcome: failed`, which verifies
  nothing — `prepare_submission` takes that branch without calling
  `verify_action_postconditions` at all — no longer strands the request in `fulfilled`,
  where `open_requests` selects on `status == "open"` and so no later order would see it
  again; the request stays open and routable. The reuse refusals, whose escapes were
  unfollowable precisely because the record had already been written, no longer speak about
  a record that would have to be walked back: a refusal now ends an order that changed
  nothing, and a later order finds the request exactly as issuance found it. And a scoped
  request's record cannot be rewritten mid-order, because the store may not move at all
  while the order is pending: the request-scope guard's mutable set is empty now, where it
  used to hold every request the action fulfilled, so an edit to a scoped request's
  `rationale` or `scope` is a refusal where it was accepted before.

  **All four hold on both acquisition arms.** Contingent bookkeeping was delegated-only when
  it first landed, and a provider acquisition order's request-scope guard still admitted
  every field of every request the order named, so the same mid-order rewrite was accepted
  there. It is not any more: an acquisition order freezes the request store whoever executes
  it, and the exemption is now a record already carrying its own committed claim.

  Narrowing that exemption could never have closed it, which is worth recording because it
  was the obvious repair. The exemption is by record id rather than by field, and fulfilment
  itself rewrote the record — so no width separated an honest fulfilment from a rewrite.
  What changed is the other side of the comparison: a fulfilment is a claim now, so an
  honest one leaves the record byte-identical and exempting nothing became correct rather
  than unusable.

  Three neighbouring behaviours are deliberately unchanged. A **research** order's `reopen`
  still writes the page straight through: scope is what sanctions a question mutation, so a
  research order legitimately reopens the questions it scopes, and no acquisition
  submission would ever come along to commit a claim on its behalf. A **provider**
  workspace is still never refused for lacking a sanction, which is what the delegation
  gate has always promised an operator working by hand: it is told which order scopes its
  change so the command can file a claim there, and the only refusal that reaches it is two
  live acquisition orders scoping one subject, which no answer resolves. And
  `record-attempt-failure` still writes the attempt audit durably, which is the right
  answer for it: an audit of what an acquirer tried is not an assertion about evidence, and
  it has to survive the refusal it documents. Only its "already fulfilled" refusal moved,
  and only to consult the claim ledger as well as the store, so a request answered by an
  uncommitted claim is refused in the same words and classifies as the same recoverable
  code.

  One consequence for anything that reads a workspace mid-order. While an acquisition order
  is pending, delegated or provider, `source_requests.py list --status open` keeps returning a
  claimed request as *open*, and goes on doing so until the submission is accepted — `list`
  reads the store, and the store is the thing that is not moving. That is the mechanism
  working rather than a lag to route around, and both commands that file claims say so
  themselves. Their JSON carries a `contingent` boolean — on every `question_resolve.py`
  envelope, false everywhere but a claimed reopen — text output appends
  `(claimed, pending acceptance)`, and `log.md` says `reopen claimed` where it used to say
  `reopened` and appends `(claimed; committed when the order is accepted)` to what either
  command records, rather than auditing a transition that has not happened. A reader that
  needs to know what the workspace holds reads that flag, not the status. Pinned by four
  propositions asserted against the durable bytes rather than against a command's return
  value: three of them — the frozen store and page, the refused submission that commits
  nothing, and the failed outcome that leaves its request routable — fail before this
  change and pass after it. The fourth was the provider arm, which did not move when the
  others did until the freeze reached it, and now asserts the refusal it used to measure as
  an admission.
- **Fix: the machine export dropped every capture a source record delivered beyond the
  first.** A manifest record can own more than one delivered path — inventory folds a
  paired paper's PDF into the LaTeX-bundle record for the same work — and each capture was
  retrieved separately, carrying its own origin URL, retrieval time, license and checksum
  as an `additional_provenance` entry. `export_answers.py` read `provenance` once and
  nothing else, so an exported citation described the first capture and stayed silent about
  the rest. A consumer reading the export document could not tell that a second capture
  existed at all, let alone where its bytes came from.

  The checksum is the part that mattered. Verification is per-capture by construction — a
  hash means something only beside the bytes it was computed from — so a paired capture
  that fails to verify records `checksum_verified: false` on its own entry and never on the
  primary's. Read through the export, such a record was indistinguishable from one whose
  single capture verified cleanly: the failure was not reported as false, it was absent.
  The workspace had already marked the record `review_required` and warned about it, and
  none of that reached the document downstream agents actually read.

  Each citation now carries `additional_provenance[]`, one entry per further capture, with
  that capture's `path`, `origin_url`, `retrieved_at`, `license`, `checksum` and
  `checksum_verified`. The key is emitted only when a record delivered more than one
  capture, matching how `checksum` and `academic` are already attached, so existing
  citations keep their shape and no other exported field moves. `request_id` and
  `candidate_id` stay out, as they are stripped from the entries themselves: which request
  authorised a delivery has exactly one answer per record, and every consumer reads it from
  `provenance` alone. `sidecar_path` stays out for the reason the primary's does — it
  locates inventory's input inside the workspace, while a citation reports where the bytes
  came from rather than which file said so. An entry that names no path is not carried at
  all but reported in `warnings[]`, because a `checksum_verified: false` against a capture
  the consumer cannot identify asserts a verdict about a file it cannot go and look at —
  and dropping it quietly would be this same defect one level down. Found by inspection of
  the export against the
  manifest contract at 0.5.2, and reproduced there: the new assertions fail with the field
  absent before the change and pass after it.
- **Change: `--reject-mismatch` now refuses a record whose secondary capture's checksum did
  not verify, where before it admitted one.** A record can deliver more than one capture:
  inventory folds a paired PDF into the LaTeX-bundle record for the same paper, and it folds
  every link file naming the same URL into one link record. Each further capture's sidecar
  becomes an `additional_provenance` entry carrying the checksum of its own path.
  `strict_checksum_refusals` read the record's primary `provenance` and nothing else, so a
  record whose second capture had been proven mismatched — `checksum_verified: false`
  against the bytes actually delivered — passed the mismatch-rejecting mode untouched. That
  is now a refusal, and a secondary capture's refusal names it: the offending `path`
  appears in the warning text and in the error envelope under `details.refusals[].path`, so
  an operator reading a refused run can tell which of the record's captures failed rather
  than only which record was dropped. A primary-capture mismatch is unchanged and still
  names the record alone, carrying no `path` — nothing on the record says which delivered
  path the primary `provenance` describes. A record with several failing captures raises one
  refusal per capture; `details.source_ids` and the envelope's count stay counts of sources.

  This is a narrowing of an opt-in flag, and it is disclosed as such. A workspace that runs
  `source_inventory.py --reject-mismatch` over multi-capture records may see it exit
  non-zero and drop a record on the next run where the previous release wrote that record to
  the manifest. That is the flag doing what its name says — the mismatch was always real,
  was always warned about, and always marked the record `review_required`; only the
  exclusion was missing. Nothing changes for a run without the flag, and nothing changes for
  a record that never grew an `additional_provenance` entry.

  **`--require-checksum` stays primary-only, deliberately.** The two flags ask different
  questions. A checksum that is present and did not verify is positive evidence about a
  specific capture, so it is now asked of every capture. A checksum that is *absent* is
  evidence of nothing, and demanding one from every capture would refuse correct
  deliveries — a secondary capture may legitimately arrive without one, and a capture whose
  target is a directory can never be verified at all, which is why the requirement would
  refuse every paired paper, whose primary capture is the bundle root. A record whose sole
  unverified checksum sits on a secondary capture is therefore still admitted under
  `--require-checksum` alone, pinned end to end by its own test.

  Rated low, not a security fix, and the severity is stated here rather than left to
  inference. The mismatch was never silent: it always warned in the report and always marked
  the record `review_required`, so no run was told the delivery was clean. Two claims that
  would have narrowed it further were checked and do not hold, and are recorded here rather
  than repeated: a multi-capture record's primary capture is *not* always a directory, and a
  link record built from two link files can carry `provenance.checksum_verified: true`
  alongside a mismatched `additional_provenance` entry, which an exported citation reports as
  the record's verification status. Every consumer other than `--reject-mismatch` — lint, the
  evidence gates, export — still reads the primary `provenance` alone; that boundary is now
  stated where it is relied upon rather than assumed away. Verified by reverting only the
  production change and confirming the refusal tests fail while both `--require-checksum`
  controls still pass.
- **Fix: a file delivered under a dot path inside a bundle was admitted by the record that
  owns it and counted by nothing.** An arXiv or LaTeX bundle record declares one `raw_paths`
  entry — the bundle directory — and no member list anywhere, so the whole subtree beneath
  it is the record's unit of admission: the controller expands that directory-shaped entry
  into every regular file beneath it with no skip predicate, and the raw tree snapshot
  fingerprints one entry per regular file the same way. `bundle_file_count`, which fills the
  record's `metadata.file_count`, filtered members through `should_skip` instead — and
  applied that predicate to the path relative to the *workspace* rather than to the bundle,
  so one dot component anywhere in the prefix suppressed the whole subtree under it. A file
  written to `<bundle>/.build/main.aux` was therefore admitted under the record and
  invisible in it: the count did not move, and `raw_fingerprint`, which filters the same
  way, came back byte-identical, so the delivery triggered no re-normalization either.

  `metadata.file_count` now counts every regular file beneath the bundle directory,
  dot-prefixed members included, which is the same subtree the record admits and the
  snapshot walks. It classifies entries the way the snapshot classifies them — `lstat`
  rather than `is_file`, a real regular file rather than a symlink to one, a link count of
  exactly one — because the snapshot refuses a symlink or a multiply-linked file rather than
  enumerating it, and counting either would put the same subset mismatch back with the
  excluded set merely moved to the other side. `should_skip` is unchanged and still decides
  which paths become *records*: dotfiles are still not inventoried as separate sources,
  because how a record is selected and how much evidence it admits are different questions.

  Narrowing the other side was measured and rejected. Teaching raw-path attribution to skip
  dot paths makes the counts agree just as well and refuses a lawfully delivered local code
  repository on its own `.git/HEAD` — the defect the local-repository entry below closes,
  arriving from the other direction. Widening the count is the direction that leaves both
  record kinds consistent with the tree the snapshot walks.

  **The fingerprint half is disclosed, not closed.** `raw_fingerprint_paths` still filters
  through `should_skip`, deliberately: it names the bytes normalization re-reads, not the
  bytes the record admits, and widening it would contradict what that field is for rather
  than repair it. A dot-prefixed member beneath a bundle therefore still reproduces a
  byte-identical `raw_fingerprint` and still triggers no re-normalization. Nor is the count
  a member list: nothing in the record bounds what a delivered bundle may contain, and
  closing that needs an inventory-level member list rather than a wider predicate here.

  Two refusals to expect for anyone holding an open order. A bundle record with a
  dot-prefixed member no longer fingerprints as it did, and the acquirer's mandatory
  `source_inventory.py --report` re-derives every record in the manifest, not only the ones
  its order fulfils. So an order issued before this change and submitted after it can be
  refused two ways. Where the rewritten record is one the order fulfils, the refusal is
  `manifest_record_changed_after_issuance`. Where it is any other pre-existing record — an
  unrelated paper whose bundle happens to carry a `.latexmkrc` — the submit fails the
  manifest scope guard first: `ORCHESTRATION_POSTCONDITION_FAILED`, "changed, removed, or
  added evidence-manifest records outside fulfilled source scope", with
  `manifest_scope_violations.changed_outside_scope` naming that untouched record. Its
  printed remediation, "Restore existing and out-of-scope manifest records", cannot be
  reached by re-running inventory, which derives the same new count again. Both are
  recoverable the same way and by the same route: issue a fresh order against the
  re-inventoried manifest and resubmit, rather than editing counts back by hand. The same
  holds for every record whose count this release moved, local repositories included.
  Reproduced on 0.5.2.
- **Fix: a stray file delivered during a blocked acquisition could be reported as a broken
  raw tree.** A `blocked` acquisition submission records a partial delivery: something was
  fetched, nothing was fulfilled. Its raw-scope guard asks inventory to attribute the
  delivered files to the manifest records the order correlates, and both consumers of that
  attribution — the `raw_paths` cross-check and the set of admitted new files — are keyed
  on the correlated records the action *appended*. A delivery that continues an earlier one
  appends none: the correlated record was already in the manifest when the order was
  issued. In that state the attribution pass has no consumer and its result was discarded
  whatever it contained, the admitted set coming entirely from the literal declared paths
  of the pre-existing correlated records.

  It was derived anyway: the gate also fired whenever the delivery had added any file at
  all beneath the configured raw source roots, which is exactly what a stray delivery does
  and what a continued one usually does. Deriving attribution re-walks and re-hashes
  the whole raw tree, and it can fail. When it failed, the raise travelled out ahead of a
  refusal that had already been decided: the operator was told "delivered raw evidence
  could not be re-derived by source inventory rules" and sent to repair a raw tree that was
  not broken, while the one file that was actually wrong was never named. The refusal that
  fits — "blocked acquisition changed raw evidence outside correlated partial deliveries",
  carrying the stray path in `unexpected_new_raw_paths` — was reachable only when the
  derivation happened to succeed.

  The gate now fires on the correlated records the action appended and on nothing else,
  which is the shape both completed arms already had — each gates on the id set its own
  consumers read, though the sets themselves differ: the completed arms use the
  controller-issued fulfilled ids, this arm the manifest additions it has just observed.
  Nothing else in the block moved. A partial delivery that does append a correlated record
  still derives attribution exactly once and is still cross-checked against it, and since
  both consumers iterate that id set alone, not one delivery's admitted paths change on any
  arm. What changes is the delivery that appends nothing: it is judged on the files it left
  rather than on whether an unconsulted derivation happened to survive. The two refusals
  that derivation could raise — a raw tree inventory cannot read, and a peer holding the
  acquisition barrier — therefore stop reaching this case, and a continued delivery stops
  paying for a tree walk whose answer was thrown away.

  Introduced with the attribution predicate described in the next entry and fixed before
  either shipped: 0.5.2 asks no derivation on this arm, so no released version can produce
  the substitution. Reproduced on this branch.
- **Fix: the memoised raw attribution could be reused for a tree it was never derived
  from, and the refusal it feeds did not say which declared path was unaccounted for.** One
  `submit` verifies the same workspace up to three times, so the inventory derivation is
  memoised for the run, and the key was the raw-tree content fingerprint alone. That
  fingerprint is a statement about regular files — the raw-tree snapshot records an entry per
  file and none for a directory — so creating an empty directory left the key byte-identical
  while changing what inventory derives from the same tree. An empty `.git/` is one of the
  markers that turns a directory into a local-repository record: on either side of that one
  `mkdir` the same tree derives a different record set, and the second and third verification
  passes were answered with the first pass's attribution.

  The memo now keys on that fingerprint composed with a digest of the directory set the
  derivation walked — directory names only, no file bytes re-hashed — so an answer is reused
  only while both are unchanged. The composition is internal to the derivation, so every call
  site still passes the one fingerprint it already holds, and one accepted submission still
  costs exactly one derivation pass. The raw-tree snapshot was deliberately not widened to
  record directories: its entries are also the raw-scope guards' universe, so a directory
  appearing among them would surface as an unexpected new raw path and refuse legitimate
  deliveries — trading a stale memo for a broken bundle acquisition.

  The refusal for a record whose `raw_paths` is not what inventory derives now carries a
  third field beside the two lists, `declared_not_derived`: the declared paths inventory
  accounts for none of, in declared order. Both lists side by side say only that they
  disagree, which on a hand-appended path left the operator to diff them by eye, and the
  standing advice — re-run `source_inventory.py --report` — does not repair a hand-edited
  record, so following it reached a second, honest refusal rather than the fix. The new
  field names the path to remove, and the advice is unchanged. It supplements the
  pinned-order equality test and never replaces it, and it is one-sided on purpose: it
  reports what a record declared that inventory accounts for nowhere, so it is empty
  whenever every declared path is accounted for. A reorder, a duplicate, and a declared
  list that omits a derived path are all still mismatches, each reporting an empty
  `declared_not_derived` — read that as "nothing surplus was declared", not as "the lists
  agree".

  Mirrored alongside, as hygiene rather than a fix: the bundle-directory expansion inside the
  derivation now applies the raw-tree snapshot's own file predicate — one `lstat`, link-like
  entries refused, regular files whose link count is not one refused — where it had used
  `Path.is_file()`, which resolves symlinks and accepts hardlinks. No behaviour changes
  today, because the snapshot refuses such a file before the expansion is ever consulted;
  that agreement was incidental, and is now stated.

  Both defects arrived with the derived-attribution predicate described in the entries below
  and were caught before any release carried them, so there is no released version to
  reproduce them on. Both were reproduced on this branch by reverting the repair and watching
  the test fail: the mismatch payload missing its field, and a derivation across one `mkdir`
  answered from the stale memo.

- **Fix: a directory-shaped `raw_paths` entry could not be delivered inside any acquisition
  order.** A bundle record — an arXiv or LaTeX source archive, a local code repository —
  declares exactly one `raw_paths` entry, the bundle directory itself. The raw-tree
  snapshot records one entry per regular *file*, and each arm built its set of admitted
  paths by adding the literal `raw_paths` string with no prefix expansion. The guard
  therefore admitted none of what the fulfilled record declared: every file beneath the
  directory came back as an unauthorised new raw path, and the refusal told the operator to
  remove deliveries the fulfilled record itself referenced.

  This broke in-order bundle acquisition on the delegated, provider and blocked-partial
  arms alike, including the `arxiv download --format source --request-id ...` form of
  `fetch_sources.py` that `docs/acquisition.md` and the acquisition skills instruct. No
  workaround kept the evidence intact: the only way to stop the record naming a directory
  is to stop the delivery being a bundle, which scatters one `raw:` record per file and
  drops `metadata.arxiv_id`. It is admitted now, by the single change described in the next
  entry. Reproduced on 0.5.2. Present by inspection at 0.5.1 — the mechanisms are
  byte-identical there, but were never executed, so that is an inspection and not a
  reproduction.

- **Hardening: a `raw_paths` list appended by hand to a fulfilled manifest record
  authorised the extra file.** The set of admitted raw paths was built from the acquirer's
  own record bytes, so the party under check also decided what the check would allow. An
  acquirer could deliver a file no inventory run had ever seen and attach it to a fulfilled
  source by editing that source's manifest entry.

  Deliberately not filed as a security fix, because the reachable effect was small and
  saying otherwise would misrepresent it. The smuggled file was inert: normalization of a
  record reads the path its kind selects, not an appended tail, so the extra bytes never
  reached the record's normalized output or its checksums, and the next
  `source_inventory.py --report` split the file back out into a record of its own. It also
  granted nothing the acquirer did not already hold — for a source it fulfils, the acquirer
  already authors both the manifest record and the normalized bytes, and neither is
  re-derived. What it did cost was the meaning of the check: an allowlist computed from the
  bytes under examination attests nothing, whatever its blast radius.

  Both defects are closed by one change: the acquirer authors the bytes, and attribution is
  derived rather than declared. A single inventory derivation over the delivered tree — one
  that writes neither the manifest nor the activity log, though taking the acquisition
  barrier does write a holder file under `raw/.locks/` — now answers both raw-scope
  questions: which new raw files an acquisition may create, and whether a new record's
  `raw_paths` is what inventory itself would derive. A directory-shaped entry admits the
  regular files beneath it, because that is what inventory
  attributes to that record; a declared list inventory does not derive is a refusal naming
  both lists. The derivation is memoised per submission by the raw-tree fingerprint, so
  verifying up to three times costs one pass.

  **The identity anchor did not move on the arms where that guard was ever the anchor.** On
  the two completed arms — delegated and provider — `allowed_new_ids` at the manifest guard
  is still exactly the controller-issued fulfilled set — the same call, unchanged by this
  release. The blocked-partial arm never read that way and still does not: it passes the
  additions it has just observed, so that guard admits any new record by construction, which
  this change neither introduced nor leans on. What bounds a partial delivery's additions is the
  correlation requirement immediately after that guard, which refuses any new manifest
  record whose `provenance` does not name this action's scoped request and candidate with
  the candidate agreeing; the raw files such a delivery may create are then attributed over
  those correlated new records alone. On all three arms every `mutable_ids` over evidence is
  still `set()`, so a pre-existing record that was changed or removed is still caught at
  that guard, and the manifest-scope guard still fires before any raw-path logic. What
  changed is per-record attribution — which files a record accounts for — not which records
  may exist.

  **This is not fully bounded, and should not be read as such.** A bundle's subtree is its
  record's unit of admission, so a file placed inside a directory the acquirer marked as a
  bundle is admitted under that record: the derivation has no member list to hold it to,
  because the record itself has none. The other half of that admission is easy to miss, so
  it is stated here: the file does not show up in the record's own account of itself either.
  Neither `metadata.file_count` nor `raw_fingerprint` is re-derived or compared by this
  check, so whatever the record declared stands; and for a dot-prefixed file beneath an
  arXiv or LaTeX bundle, re-running inventory reproduces both byte-identically anyway,
  because both filter through `should_skip` while admission does not. A local repository
  record carries no `raw_fingerprint` at all. So such a file triggers no re-normalization,
  and `raw_fingerprint` must not be read as "the bytes this record stands for": it is the
  bytes normalization re-reads, which for a bundle is a subset of what the record admits.
  Closing that needs an inventory-level member list rather than a controller change, and it
  stays open on purpose. Separately, the normalized bytes of a newly fulfilled record are
  still trusted as delivered — only reused sources are re-derived and compared — and that
  boundary is unchanged by this release.

  Two behaviour changes follow for anyone driving the protocol. A manifest record inventory
  cannot re-derive is now refused where it previously passed, so a record an acquisition
  creates has to come from running `source_inventory.py` rather than from an editor. And a
  derivation that cannot run at all is a new recoverable refusal rather than a crash,
  repaired by making `source_inventory.py --report` succeed and resubmitting.

- **Fix: a manifest record that owned two delivered captures kept only the first one's
  provenance.** Inventory folds a paired PDF into the LaTeX-bundle record for the same
  paper, so one manifest row can own two paths that were retrieved separately, at different
  URLs, and hash differently. `fetch_sources.py` writes a sidecar beside each of them, and
  only the first matching sidecar was merged: the second was reported as "additional
  provenance sidecar not merged" and then discarded, taking the paired capture's
  `origin_url`, `retrieved_at` and verified `checksum` with it. Nothing in the manifest
  said where the PDF had come from. The bytes were never unaccounted for — both sidecars
  already counted toward `raw_fingerprint`, so a correction to either still re-triggered
  normalization — but the parsed fields were dropped, and no test exercised the path at
  all. Every matching sidecar is merged now: the first still becomes `provenance`,
  unchanged in shape, selection and checksum handling, and each further one becomes an
  entry in a new record-level `additional_provenance` list that names the `path` it
  describes and is checksum-verified against that path's own bytes rather than the
  primary's. A sidecar matching no record at all is still reported as unmatched.

  The correlation fields are deliberately stripped from those entries.
  `provenance.request_id` is the only link between a delivered capture and the source
  request that authorised it, and delegated fulfilment correlation reads it — with
  `candidate_id` — as a scalar. A paired capture carrying a second copy would turn one
  authorisation into an ambiguous pair, so correlation goes on reading the primary
  `provenance` alone and this change moves nothing about which deliveries are authorised.
  For the same reason the second sidecar's fields are not folded into the first one's
  mapping: a `checksum` means something only beside the bytes it was computed from, and a
  merged mapping would state a verified hash about the wrong file.

  One consequence for anyone holding an open order. A record that gains
  `additional_provenance` no longer fingerprints as it did before, so an order issued
  before this change whose acquirer re-runs `source_inventory.py` and then submits can see
  such a record refused as `manifest_record_changed_after_issuance` — the rewritten-since-
  issuance cause, which is recoverable. Issue a fresh order and resubmit rather than
  editing the manifest back.

- **Fix: a local code repository could be stamped `bounded: true` and then refused as
  unbounded.** `codebase_intake.bounded` was decided by a file count that filtered members
  through `should_skip`, which withholds every dot-prefixed path, while the raw-tree
  snapshot that consumes the promise fingerprints every regular file beneath the raw roots
  and refuses the workspace with `ORCHESTRATION_WORKSPACE_UNSAFE` past its own 10,000-entry
  limit. Two identical caps over two different sets: a 9,000-file checkout carrying a
  3,000-object `.git` passed the intake bound and was then refused by the guard, the
  refusal naming a tree the record had already declared admissible. `.git` is itself one of
  the markers that makes a tree a repository, so the excluded subset was not an exotic case
  — it is what every acquirer clones.

  `metadata.file_count` and `metadata.codebase_intake.file_count` now count every regular
  file beneath the repository directory, which is the same subtree the record admits and
  the snapshot walks. Both still stop one past the intake limit rather than enumerate a
  tree already refused, so a repository over the bound publishes `file_count: 10001` and not
  its true total: past the limit that field is a verdict, not a census. `should_skip` is
  unchanged and still decides which paths become *records*: dotfiles are still not
  inventoried as separate sources, because how a record is selected and how much evidence it
  admits are different questions.

  **That subset mismatch is closed for local repositories, and for bundle counts by the
  entry above.** The same shape survived one record kind over: `bundle_file_count`, which
  fills an arXiv or LaTeX bundle's `metadata.file_count`, filtered through `should_skip` over
  the same directory the raw-tree snapshot walks unfiltered, and it is widened in this same
  release. Nothing contradicted itself there the way it did for repositories, because a
  bundle record carries no `bounded` flag to be contradicted — the effect was quieter rather
  than absent: the count stated less than the tree the record admits.
  `raw_fingerprint_paths`, which decides what a `paper` record's `raw_fingerprint` covers,
  still filters that way and is deliberately left as it is, so a dot-prefixed file beneath a
  bundle still falls outside the fingerprint that decides re-normalization.

  **This is not a workspace-wide bound, and should not be read as one.** The snapshot's
  limit totals across every configured raw source root while the intake limit is per
  repository, so several correctly bounded checkouts can still add up past it. Closing that
  needs a workspace-wide accounting rather than a different predicate in inventory, and it
  stays open on purpose.

  Two consequences for anyone holding an open order. A local repository record containing
  dot-prefixed files no longer fingerprints as it did, so an order issued before this change
  whose acquirer re-runs `source_inventory.py --report` and then submits can see that record
  refused as `manifest_record_changed_after_issuance` — recoverable; issue a fresh order and
  resubmit rather than editing the manifest back. And a repository whose `.git` carries it
  past the limit now reports `bounded: false` with `review_required` where it previously
  reported bounded, which is the refusal arriving at intake instead of at submit.

- **Fix: two more reuse-path remediations that were refused for being followed.** 0.5.2
  closed that defect class for the `REUSE_SCOPE_*` causes only. Reconciliation held a reused
  source to one of two arms and attached one shared remediation to both, and that
  remediation named the record rewrite: the right recourse on the arm where the order
  recorded no normalized output, and advice that cannot succeed on the arm where it recorded
  one. Reuse there reconciles against the exact bytes the order fingerprinted, and every
  normalization run restamps the second-resolution `normalized_at` those bytes include, so
  following the printed advice returned the identical refusal, as many times as it was
  followed.

  What a host parsing the refusal envelope should expect. Each entry in
  `details.reconciliation_failures[]` now carries its own `repair`, keyed to the arm and the
  state that entry is in: the arm whose normalized bytes the order fingerprinted, the arm
  that owes a record re-derived from unchanged raw evidence, and a third for that second
  arm's failures where the re-derivation could not be performed at all, so the record's
  bytes were never what failed. The terms common to both arms are stated once and point at
  that field, the shape the reuse-scope refusal already had, on the delegated and provider
  arms alike. The refusal message and the `was_scoped_match`, `was_authorized_unnormalized`,
  `derivation_checked` and `derivation_failure` keys are unchanged.

  Adapter identity was the second refusal. `frontmatter_for` derives `normalizer.name` from
  the adapter `research.yml` configures, while `carry_version_stamps` deliberately carries
  only versions, so a stamped name that disagreed was just different bytes: it came back as
  "normalized evidence is not what normalizing the raw evidence produces", whose remediation
  is about a hand-edited body — sending the operator to hunt for a prose edit while the two
  lines that actually disagreed sat in the frontmatter and were never quoted back. The name
  is now compared explicitly, before rendering, and refused under its own
  `derivation_failure.reason`, "normalized evidence does not name the normalizer configured
  for its kind", carrying the recorded `normalizer` and the `configured_normalizer` in keys
  of their own. Separately, an adapter that raises `AdapterError` — most sharply, one
  reporting a program identity `research.yml` does not authorize — no longer has its message
  buried in the blanket clause's `error` string: it reports "the normalizer adapter this
  workspace authorizes did not produce a record to compare against", which is one of the
  verdicts a record rewrite cannot clear. Both reason strings are new, so a host matching on
  `derivation_failure.reason` will see two values it has not seen before.

- **Fix: refusing to relink a fulfilled request was reported to hosts as a broken
  workspace.** `source_requests.py fulfill` refuses to point an already-fulfilled request at
  a second source — an ordinary refusal of an ordinary mistake. Its message matched no
  clause in `classify_error_code`: the clause written to catch it tested for "already
  fulfilled by a different source id", a string this package has never raised anywhere. The
  refusal therefore fell past every clause to the classifier's `WORKSPACE_UNREADABLE` tail,
  which is declared non-recoverable and remediated as checking the workspace path and its
  required starter files. A host was told the workspace was unreadable and must not be
  retried, when nothing was wrong with the workspace and one relink simply was not allowed.

  It classifies as `REQUEST_ALREADY_FULFILLED` now. A host branching on this envelope reads
  `recoverable: true` where it previously read `false`, and that code where it previously
  read `WORKSPACE_UNREADABLE` — the opposite direction to the five codes 0.5.0 declared
  non-recoverable, and correct for the same reason those were: it is what the refusal always
  meant. That code's registry remediation was written for `record-attempt-failure` alone and
  now answers both commands that reach it, since a fulfilled request accepts neither a
  recorded attempt failure nor a relink. Re-fulfilling a request with the *same* source id
  is unchanged and still succeeds idempotently. The tests now assert `error_code` and
  `recoverable` rather than a stderr substring, which is what let the mismatch ship.

- **Fix: the reuse and reconciliation refusals stopped advising commands that refuse.**
  Every escape those refusals printed was unfollowable in the only state that could print
  it. Both the reuse-scope failure set and the reconciliation loop are computed over the
  request store's own `fulfilled` records, so the request under discussion is already
  fulfilled by the very source being refused — and `source_requests.py` closes both doors
  out of that state: `record-attempt-failure` refuses a fulfilled request outright, and
  `fulfill` refuses to relink one to a later delivery. The remediations advised one or both
  anyway, as did two of the three per-cause repairs and the delegated correlation refusal.
  An operator who followed the printed advice reached a second refusal for having followed
  it.

  Both doors were walked in tests rather than reasoned about, and neither is named now.
  What the refusals state instead is the fact underneath all of them — a fulfilled request
  has no second route — and, where the per-source repair cannot be performed, that this
  order has none either. Two more escapes were found the same way and removed: the provider
  arms' "acquire it through another selected candidate" bottoms out at that same relink
  refusal, and `manifest_record_changed_after_issuance`'s "restore it exactly" is required
  by the scope guards but never cleared this refusal, because membership in the order's
  reuse baselines was fixed at issuance and no rewrite ever moved it — restoring the record
  only changes which of the other two causes gets reported. The delegated acquirer skill,
  which routed a refused reuse to both dead escapes, is corrected to the same effect.

  One more `repair` changes, one level down and for the same reason. A re-derivation that
  *crashes* reports the blanket reason "normalized evidence could not be re-derived from the
  raw evidence", and that reason was missing from
  `UNVERIFIABLE_DERIVATION_REASONS`, so it was answered with the record rewrite — which
  re-enters the same normalizer that had just raised. It joins that set, so such a failure
  now carries the repair pointing at `derivation_failure.error` instead.

  **The `failed` outcome is disclosed here, not closed.** The `scoped_match` repair used to
  offer ending the action with a failed outcome and starting a fresh session, as though that
  were a clean exit. It is not one, and nothing refuses it: `prepare_submission` answers a
  `failed` outcome without calling `verify_action_postconditions` at all, so no evidence or
  scope check runs. The fulfilment the acquirer already wrote stays in the request store with
  its `source_id`; `open_requests` selects on `status == "open"`, so no later order sees that
  request again; and evidence the controller had just declined to verify is accepted
  permanently. The repair names that cost now instead of naming the command, and a test
  performs the outcome and observes each part of it. The hole itself is unchanged and stays
  open on purpose.

## 0.5.2 - 2026-08-19

- **Fix: two concurrent status reads could make each other fail.** Writing the status
  cache back renamed from a temporary whose name was fixed, so two callers refreshing it
  in one workspace wrote the same path: the first rename moved it away and the second
  raised `FileNotFoundError` from a call that had only asked to answer a status query. The
  temporary is now unique per writer, and a write that loses the race is no longer fatal --
  the cache is an optimisation, and a concurrent writer's document is as valid as this one.
  Present since before 0.5.1.

- **Security fix: postcondition verification could be made to run a program of the
  acquirer's choosing.** Re-deriving a reused PDF record read `pdf_extractor.name` out of
  the normalized file — the file the untrusted acquirer had just written — and passed it
  through as a bare string. In the extraction library a bare string has one meaning, *a
  resolved `pdftotext` executable path*, so the stamped value became `argv[0]` of a
  subprocess run with the workspace as its working directory, in the middle of the check
  that exists to decide whether the acquirer's output can be trusted. The extractor
  allowlist was never consulted.

  The same line also made the feature unusable in the direction it was written for. A
  record this package produces stamps `pypdf`, and there is no executable by that name, so
  every legitimate reuse of a PDF source failed as "normalized evidence could not be
  re-derived from the raw evidence" — a refusal blaming the acquirer for the verifier's own
  bug.

  The stamped name is now resolved through the allowlist and reaches the extractor as a
  real extractor identity. A name this package does not implement is a refusal naming it,
  and an extractor this host does not have is a separate refusal naming that; neither
  attempts to execute anything. Reported downstream as EW-BUG-005.

- **Fix: verification could refuse an acquisition for a file verification itself had just
  written.** Re-deriving a reused record runs the workspace's configured normalizer
  adapter — an external program — and it ran before the post-action snapshots were taken,
  with the workspace as its working directory. Anything it wrote on the way past was
  attributed to the acquirer: a scratch file under `raw/` as immutable evidence changed, one
  under the normalized tree as evidence rewritten, one under `docs/` as a tampered trusted
  input. Deleting the file and resubmitting produced it again, so the refusal could not be
  cleared and named the wrong party every time round.

  Both post-action snapshots are now read before that check runs, and an adapter
  re-derivation is confined to a bounded throwaway copy holding the trusted static inputs
  and the one record's own raw evidence. Built-in extractors, which write nothing, still
  re-derive in place.

- **Fix: reuse verification held the evidence to the host it ran on.** `normalizer.version`
  and `pdf_extractor.version` are stamped from whatever is installed at the moment of the
  run and were part of the compared bytes, so an upgrade between issuance and submission, a
  different virtualenv, or an order replayed across a version bump turned a legitimate reuse
  into an accusation that the record had been hand-written. Both are now read back, the way
  `created`, `updated` and `normalized_at` already were, matching this package's own
  treatment of extractor versions as provenance rather than a rewrite trigger. The producer
  *names* stay derived, so a record still cannot claim a producer that did not produce it.

- **Fix: an acquisition order could be issued that no submission could complete.** The
  baseline of reusable un-normalized sources admitted a kind the derivation check refuses
  unconditionally, so the order required a normalized output and then refused the same
  output; and the two reuse baselines were computed over two separate reads of the manifest,
  so a file appearing or disappearing between them could put a source in neither or in both
  — the second of which made every submission of that order fail as an invalid baseline.
  Both baselines now come from one read and share one predicate about which kinds reuse can
  cover. Re-derivation is also refused outright when a record names an input path outside
  the fingerprinted raw roots, rather than confirming the body against bytes no baseline
  pins, and cited `references_source_ids` are admitted only as ids the evidence manifest
  holds.

- **Fix: remediations that were refused for being followed.** The reconciliation refusal
  advised re-running `normalize_sources.py --force`, which selects nothing, and the
  missing-record refusal advised `--all`, which normalizes sources the order does not scope
  and is then refused for doing so. Both now name `--source-id`, which repairs exactly the
  record the refusal is about. Each cause of the reuse refusal carries its own repair
  instead of one shared sentence that, for a correctly correlated source, described the
  state being refused. Both refusals are also split per acquisition arm, so the provider arm
  is no longer told to record an attempt failure its own fulfilment guard forbids, and the
  provider arm now reports reuse before the guards that used to mask two of its three
  causes. Selecting a source this package has no extractor for now refuses as
  `SOURCE_NOT_NORMALIZABLE`, with a repair of its own, instead of reporting the whole
  workspace unreadable.

- **Fix: a submission re-derived every reused source three times.** `submit` verifies three
  times by design — to prepare, to confirm, and to apply — and each pass re-ran the full
  re-derivation, holding the driver session lock throughout while peers were refused
  immediately. The verdict is now reached once per source per submission, keyed by the exact
  bytes it is a statement about. The published work-order schema's baseline bounds were also
  raised to cover what the controller emits and accepts, so a host validating an order can
  no longer reject one the controller considers valid.

- **An acquisition order can now fulfil a scoped request from a source it already
  correlated to that request but nothing had normalized yet.** An acquirer that delivered
  and inventoried evidence under an order that then failed, timed out, or rolled over
  leaves exactly that state: the sidecar names the request, the manifest holds the record,
  and only the normalized output is missing. Producing it is the work the next order asks
  for, and the postcondition refused it — first as an uncorrelated reuse, then as an
  unauthorised normalized file. There was no workaround. The source id is already in the
  manifest, so re-delivering the same bytes at a second path creates a different record and
  leaves the original fulfilling nothing.

  An acquisition order now also records the ids of pre-existing manifest sources whose own
  `provenance.request_id` named one of its scoped requests when it was issued and that
  nothing had normalized. Reconciliation requires such a source's manifest record to be
  byte-identical to what the order fingerprinted, and its normalized output to be newly
  written — and, because "new" is a statement about a path rather than about a body, the
  verifier re-normalizes the unchanged raw evidence itself and compares the result, record
  and structured-view sidecar alike. Both postcondition arms take it, so the delegated and
  provider arms still admit reuse on the same terms.

  **The predicate is correlation, and only correlation.** A source stamped for a different
  request, or for none, is still refused, and this change deliberately does not open it.
  Correlation is written by the acquirer, and so is every other field such a decision could
  read — the delivery's scope, its timing, its retriever — so a predicate over delivered
  metadata authorizes whatever the untrusted party decided to write. Reuse across requests
  needs an authorization from a trusted party, which this package does not yet have; until
  it does, the answer for that evidence stays a second delivery under its own raw path.
  Where the source id is stable across deliveries a second delivery cannot produce a
  distinct record, and an attempt failure is the default answer. A second delivery *form*
  exists for an arXiv `paper:` and for a `link:`, under preconditions
  `docs/source-delivery.md` states, and does not exist at all for a GitHub `codebase:`
  source. That residual gap stays open on purpose.

  **No mutable set widened, and `raw/` is still immutable.** Every `mutable_ids` in both
  arms is still `set()`. What widened is `allowed_new_ids` — what an action may *create* —
  by exactly one normalized record per reused source, and only for a source the order
  recorded as not yet normalized; one that was already normalized still authorizes no new
  file at all and still answers to byte-identity. The list is computed once, at issuance,
  from state that was already durable then, by the controller, and lives in the protected
  baseline sidecar, so nothing an acquirer writes during the order can add to it. A reused
  source whose manifest record, raw sidecar or normalized output changed afterwards is
  still refused naming the source, and no `provenance.request_id` is ever restamped. An
  order issued before this change carries no list and replays exactly as it did, with the
  reuse simply unavailable. Reported downstream as EW-BUG-005.

- **Fix: a delegated acquisition that fulfilled a request from evidence the workspace
  already held was refused five times over, and never for the reason it was refused.** The
  first refusal said the fulfilled evidence carried no provenance sidecar naming its
  request, and told the acquirer to stamp `request_id` into the sidecar and re-run
  `source_inventory.py`. Doing exactly that on a source `raw/` already holds edits
  immutable raw evidence and rewrites a manifest record the order may not touch, so the
  obedient acquirer was refused again — by manifest scope, then reconciliation, then
  normalized scope, then raw scope. Four more `ORCHESTRATION_POSTCONDITION_FAILED`
  envelopes, each describing a different artifact, none of them naming the constraint that
  was actually broken.

  The blast radius is any delegated workspace whose acquirer can satisfy a scoped request
  from evidence it delivered earlier — the reuse path
  `matching_normalized_source_records` exists to keep open. An operator holding the
  cascade had no way to tell that the real answer was "this source was never correlated to
  this request, and cannot be now"; the advice printed with the first refusal led directly
  into the other four.

  Delegated acquisition now refuses this once, up front, naming the reuse constraint: a
  pre-existing manifest record may satisfy a scoped request only when the order itself
  named it at issuance, unchanged since. Three different states fall outside every baseline
  the order wrote and their repairs differ, so the refusal reports which one it hit per
  source rather than asserting a reason: `provenance_names_no_scoped_request`,
  `no_reuse_authorization_at_issuance`, and `manifest_record_changed_after_issuance`. The
  remediation covers each — deliver the evidence as a new source under its own raw path
  with its own sidecar when nothing correlated it, restore a record rewritten since
  issuance, and record an attempt failure where neither is available. (The entry above
  closes the second of those states for a correctly correlated source; a source correlated
  elsewhere stays a dead end, deliberately.)
  Two existing remediations were corrected to
  stop pointing at the dead end: the correlation refusal now says the sidecar must be
  stamped *at delivery* and that raw immutability is why a source the manifest already
  holds cannot acquire one afterwards, and the reconciliation refusal now names
  correlation-at-issuance rather than merely asking for "the unchanged scoped
  pre-existing source".

  **No guard was relaxed and no scope was widened.** `research.yml` declares
  `raw: immutable: true`, and both fixes proposed downstream would have required an
  untrusted acquirer to retro-edit a delivered provenance sidecar — which
  `skills/research-acquire-delegated.md` forbids in as many words. The four scope guards
  keep exactly the sets they had; the change is one earlier refusal and two corrected
  remediations. Reported downstream as EW-BUG-005.

## 0.5.1 - 2026-08-17

- **Fix: an acquisition action could not fulfil a source whose normalized record binds a
  structured-view sidecar.** Normalization writes the sidecar beside the record, so
  fulfilling such a source adds two files under `sources/normalized/` — but the
  postcondition guards allowed exactly one, the record, and judged the package's own write
  an unauthorised change. Every affected submission refused with
  `ORCHESTRATION_POSTCONDITION_FAILED`, naming the file the normalizer had just created.

  This blocked delegated acquisition outright for any workspace whose evidence normalizes
  structurally, and it was never limited to normalization adapters: a plain CSV takes the
  native table path and earns a structured view too, so provider-mode acquisition of a
  table hit it just the same.

  All three scope guards — delegated acquisition, provider-mode acquisition, and the
  blocked-action path — now allow a record's structured-view sidecar, and only when the
  record itself declares one. An undeclared sidecar is still refused, as is any other
  normalized output no fulfilled source accounts for; the guards grew by at most one
  derived path per source, not by a directory. Reported downstream as EW-BUG-004.
## 0.5.0 - 2026-08-17

- **Every error code this package raises now carries a specific remediation, and every
  one is documented.** Before this release, 97 of the codes a workspace can emit fell
  back to `"Read the message, fix the input or workspace state, and rerun the command."`
  — an operator holding one of those refusals was told nothing about how to fix it — and
  37 appeared in no documentation table at all. All 194 raised codes now have a
  remediation naming an action that a real command can perform, and a row in the
  orchestrator handoff tables. `test_every_raised_error_code_has_a_specific_remediation_and_a_doc_row`
  keeps it that way.

  Eight remediations that already shipped were found to name commands that do not exist
  or that refuse without further arguments — `run_controller.py recover` without
  `--run-id`, `override-manual-url-budget` without its required flags, and a *reviewed*
  handoff-table cell telling operators to "reopen the request" when no command can reopen
  one. Those are corrected, and
  `test_remediations_name_only_commands_that_exist` now checks every command and flag
  named in a remediation against the real CLI surface.

  Two crash paths were fixed in the process: a UTF-16 `--standards-metadata` file raised
  `UnicodeDecodeError` instead of `ACQUISITION_METADATA_UNREADABLE`, and a malformed
  sidecar raised `yaml.YAMLError` instead of `SIDECAR_INVALID`. Both escaped as tracebacks
  rather than refusal envelopes.

- **`recoverable` now answers the same way every time a code is raised.** Nine codes
  reported both values across their own raise sites — `ORCHESTRATION_STATE_INVALID`
  answered three ways across 25 — so a host branching on `recoverable` retried some
  occurrences of a code and not others, with nothing in the envelope explaining the
  difference. `CANDIDATE_STORE_INVALID`, `ORCHESTRATION_OWNER_MISMATCH`,
  `ORCHESTRATION_STATE_INVALID`, `PROVIDER_REGISTRATION_INVALID` and
  `WORKSPACE_UNREADABLE` are now declared non-recoverable, so a host that previously read
  `recoverable: true` for them will now read `false`; that was already what most of their
  raise sites reported. `ACQUISITION_PATH_UNSAFE`, `CONFIG_INVALID` and
  `PROVIDER_NOT_REGISTERED` are recoverable at every site — each is an input an operator
  corrects. The two `default_recoverable` implementations are now checked against each
  other rather than mirrored by hand.

- **One error code no longer gives opposite instructions depending on where it was
  raised.** `ACADEMIC_PROVIDER_REQUEST_LEDGER_INVALID` told four operators to *repair* the
  provider-call ledger and three not to touch it by hand, for the same artifact. That
  ledger enforces provider budgets, so hand-repair is what must not be advised; all seven
  sites now carry the strict guidance, matching the registry entry that already said so.

- **New error code, and one condition now reports it instead of
  `ORCHESTRATION_WORKSPACE_UNSAFE`.** A host matching on
  `ORCHESTRATION_WORKSPACE_UNSAFE` will no longer see the case where workspace health or
  HIGH validation findings changed *after* the work order was issued; that now raises
  `ORCHESTRATION_WORKSPACE_HEALTH_CHANGED`, beside its siblings
  `ORCHESTRATION_DELEGATION_CHANGED` and `ORCHESTRATION_PROVIDER_POLICY_CHANGED`. The
  remaining `ORCHESTRATION_WORKSPACE_UNSAFE` sites are unchanged.

  One code was covering two conditions whose correct advice is opposite. Seven sites mean
  "the workspace is unreadable, changed under us, oversized, or not a regular file" —
  repair it and retry — while this one means the baseline moved, where replaying the same
  action cannot succeed. The shared registry remediation said "request the same next
  action again", which was actively wrong for the second, and `recoverable` answered both
  ways for the one code so an automated caller retried it or not depending on which
  internal path fired.
- Two remediations widened to describe both conditions their code covers.
  `ACQUISITION_LIMIT_EXCEEDED` said "lower the requested count", which does not help when
  the run has already spent its download budget; it now also names starting a new run.
  `ACQUISITION_RESPONSE_INVALID` said "retry later or inspect the provider response",
  which is wrong when the message names a broken transport adapter rather than the
  response; it now says so. Both are floor text a host reads through
  `remediation_for()`; the per-site inline messages were already correct.

- Give `reopen` the strictness layer its disclosure implied, and stop enforcing three
  package guarantees by memory. The tie disclosure above reports a pairing declared
  scope did not determine, but left every host to invent its own gate over the
  `warnings` array — and the obvious gate (`warnings == []`) is wrong for a
  workspace whose same-facet requests are interchangeable and right for one whose
  requests are merely under-scoped, a distinction only the host can draw because
  scope values are never interpreted here. `reopen --require-decisive-scope` refuses
  such a reopen with `REQUEST_SCOPE_UNDECIDED`, naming the undecided pairs and
  leaving the question `blocked`. It is the counterpart to `fulfill --require-scope`
  on the axis `reopen` has: that flag asks whether the delivery *stated* the
  request's keys, this one whether the declared keys *discriminate*. Absence
  strictness still has no equivalent on `reopen`. Opt-in, so default behavior is
  unchanged; requests that declare no scope produce no pairs and are outside the
  check, exactly as a request with no keys is outside `--require-scope`.
- `questions.reopen` accepts `require_decisive_scope` too, so the in-process door and
  the CLI agree about whether a tie-broken reopen is refused. The flag reached the
  parser, the dispatch and the `run_reopen` seam but not the published facade, which
  made `library-api.md`'s promise that an operation "does not change what it means"
  between doors false for it.
- Three consistency rules that were previously conventions are now tests.
  `test_no_shipped_surface_teaches_a_retired_scope_example` sweeps every tracked
  surface for retired example *values* — the check that replaces the one-shot grep
  which matched two syntaxes and missed the JSON form of the same value in
  `mcp-server.md`. `test_dispatch_seam_forwards_every_cli_flag_to_its_seam` compares
  each subparser's flags against the keywords `dispatch_seam` forwards, after
  `--require-decisive-scope` was parsed by the CLI, dropped at the seam boundary,
  and silently ignored while the library seam honoured it.
  `test_library_facade_forwards_every_seam_keyword` pins the next boundary out —
  every seam keyword reachable from the facade, and every accepted keyword actually
  passed on — after the same flag was found missing there too. The repo had
  `sync_vendored_scripts.py --check` for template↔mirror drift and `llm-wiki lint
  --strict` for code↔wiki drift; these close the doc↔doc, CLI↔seam and seam↔facade
  equivalents.
- Each of those guards now derives its own coverage instead of listing it, after the
  first versions were found to protect only the case that prompted them. The facade
  guard walks 18 door→seam bindings across all seven namespaces and the `Workspace`
  handle, in both call shapes, rather than eight hardcoded for one namespace; it also
  reads positional-or-keyword parameters, not just keyword-only ones. The set of
  scripts required to appear in the JSON Output Scripts table is derived from the
  scripts directory rather than a hardcoded list that silently omitted eight
  qualifying scripts, `orchestration_controller.py` — the largest error surface in
  the package — among them; remaining exemptions are declared with a written reason.
- Fix a Markdown table parser in the error-envelope checks that split rows on a bare
  `|` and ignored `\|`. Rows whose JSON-mode column reads `next\|submit\|…` had a
  fragment of the wrong column parsed as their error codes, so
  `test_json_output_scripts_table_uses_stable_error_codes` was passing while
  examining 85 of 131 codes and one orchestration code instead of 22. No shipped
  behavior changes; the check simply now sees what it always claimed to.
- Stop `reopen` from crediting declared scope for a pairing that argument order
  decided, and say which scope keys are worth declaring in the first place.
  Request scope narrows the sources that can answer each request, but it does not
  always narrow them to one: when several supplied requests declare the same scope,
  the assignment among them came from the order the requests and sources were
  supplied. That holds even when the deliveries differ, because scope is symmetric
  between those requests — whichever one ends up with the better-corroborated
  source got it by supply order, not because scope chose it. That is the positional
  guess the feature exists to replace, and it was reported as `pairs` and written
  to `log.md` as "Paired by declared scope" with nothing distinguishing it. Each
  `pairs[]` entry now carries `decided_by` (`scope` when the declared scope
  determined it, `tie_break` when another equally corroborated source could have
  answered the request or another request could have taken the source), the reopen
  report carries a `warnings` array whose `request_scope_pairing_tie` entries name
  both the alternative sources and the contending requests, and `log.md` records
  tie-broken pairs as such. A tie is reported, never refused:
  refusing would break reopens that succeed today, and `reopen` reports a pairing
  rather than recording a fulfilment. Preferring a source that corroborates more of
  a request's scope over one that merely fails to contradict it remains a scope
  decision and is not a tie. Both report fields are additive and always present;
  scope matching, request records, sidecars, and every schema version are
  unchanged, and a workspace where nothing declares scope sees no behavior change.
- Document which identifiers are eligible to be scope keys. `docs/source-delivery.md`
  gains "Choosing scope keys": the delivering side must be able to derive the value
  (a workspace-only value such as a question slug can never be stamped, and
  `--match-scope` does not exempt it because asserted keys join the required set),
  the value must vary across the set `reopen` pairs (a per-question value
  discriminates nothing), and both sides should emit it from one generator because
  values are compared as exact text. The canonical example no longer pairs
  `facet_id` with a per-question `candidate` key, which taught a key set that
  `fulfill --require-scope` cannot satisfy. `REQUEST_SCOPE_MISSING` remediation now
  offers the third branch this leaves out — drop a key the delivering side cannot
  derive — alongside stamping the sidecar and rerunning without the flag.

## 0.4.1 - 2026-08-15

- Let a pack author find out before shipping whether the mapping-only record rule
  affects their pack. `0.4.0` made `record/...` rule paths mapping-only, including
  present values previously reached through array indices, but `pack validate`
  accepted such a declaration and reported nothing that distinguished it from one
  that resolves, so the answer arrived instead as every candidate failing closed
  after deploy with a reason naming the evidence. The per-rule summary that
  `evidence-wiki pack validate` and `evidence-wiki contract` both publish now
  carries `record_fields_that_may_traverse_arrays`, naming every `record/` field a
  rule reads whose path contains a segment shaped like an array index, and the
  `policy_rules` check counts them in its message. This is reported and never
  refused: a numeric segment is an ordinary mapping key whenever the structured
  view carries one, so rejecting these paths statically would reject packs that
  resolve correctly, and only the container reached at answer time can tell the two
  apart. The list is complete for the case that matters — a path that reaches its
  value through an array carries an index-shaped step by construction — so an empty
  list means the hardening cannot reach that rule. Runtime behavior, the check's
  `pass` status, and every schema version are unchanged; the new field is additive.
  `policy_rules` documentation now also states that a numeric segment in a `record/`
  path is a mapping key and never an array index.

## 0.4.0 - 2026-08-15

- Make question-scoped policy parameters writable through every supported
  intake route and add a narrow, fail-closed review path for mechanically
  optional evidence fields. Question batch schema `1.0` now accepts bounded
  JSON-shaped `metadata`, persists it below the question frontmatter namespace,
  distinguishes same-text candidate questions by canonical metadata, and adds
  `item_index` correlation without echoing values; omitted metadata retains the
  legacy text-only behavior. Domain-pack field primitives may declare
  `when_absent: manual_review` for a missing terminal `record/...` member under
  fully resolved mapping parents in a valid hash-bound structured view. Missing
  parents, arrays, corrupt evidence, provenance fields, and missing question
  operands remain hard failures; null and blank values are present and retain
  each primitive's ordinary semantics. Tri-state composition and multi-source
  rollup keep failure dominant. Record rules are mapping-only in this runtime,
  including present values previously reached through array indices. Pack
  validation and the installation contract expose `manual_review_on_absence`, and required facets
  that can take that route require `domain_pack.human_gated: true`. The starter
  advances from `0.6.0` to `0.7.0`; workspace/research, intake, installation,
  coverage, and policy-result schema versions remain unchanged because their new
  fields are optional or additive. The mapping-only array rule is an explicit
  fail-closed hardening exception: packs that previously traversed arrays must
  normalize those inputs to mappings before upgrading.

- Pin the text encoding and line ending on every file the package reads, writes,
  or decodes from a child process, fixing silent Windows corruption. Text I/O
  previously fell back to `locale.getpreferredencoding(False)`, which is UTF-8 on
  macOS and Linux but the ANSI code page (typically cp1252) on Windows outside
  UTF-8 mode, while every reader in the package decodes UTF-8 explicitly. On a
  Windows workspace this meant normalizing a source whose text left cp1252 raised
  `UnicodeEncodeError` mid-write, and text cp1252 could encode was written as
  cp1252 bytes the UTF-8 readers then refused; `research.yml` was read with the
  locale in eight scripts and as UTF-8 in six others. `write_text` also rewrote
  `\n` to `os.linesep`, so generated artifacts differed byte-for-byte between
  platforms even though they are hashed, diffed, and line-compared. All shipped
  scripts, the library, and the repository tools now pass `encoding="utf-8"` and
  `newline="\n"`, and text-mode subprocess calls decode as UTF-8 with
  `errors="replace"`, matching the convention `_normalizer_adapter.py` already
  used. A new contract test scans the shipped sources and fails on any
  unqualified text read, write, or text-mode subprocess, so the class cannot
  return; CI could not have caught it, because `PYTHONUTF8: "1"` masks it there.
  Behavior on macOS and Linux is unchanged. Exception handling is deliberately
  untouched: readers that convert `OSError` into a diagnostic still do not catch
  `UnicodeDecodeError`, which remains a separate robustness question.

## 0.3.1 - 2026-08-13

- Add an explicit, fail-closed domain-pack lifecycle for existing workspaces.
  Fresh initialization now records pack-owned configuration and file provenance;
  legacy 0.3.0 workspaces can opt into that boundary with `evidence-wiki pack
  adopt`, and `evidence-wiki pack refresh` performs comment-preserving three-way
  reconciliation with path-specific conflict resolution, dry-run reports,
  workspace locking, retained backups, interruption journals, and deterministic
  recovery. Status, doctor, fleet status, upgrade warnings, and the installation
  contract expose the same additive lifecycle health and revision identities.
  The compatible starter capability set advances from `0.5.5` to `0.6.0`.

- Correct upgrade documentation and help: write mode uses `.locks/`, may update
  `workspace-system.yml`, and appends `log.md` only after material managed-file or
  starter-version changes while preserving prior history and user data. Dry-run
  writes nothing.

## 0.3.0 - 2026-08-11

- Close five spellings of catastrophic backtracking that a pack's `regex` rules could
  ship past `pack validate`. The declaration-time guard exists because the text a
  pattern runs against is the *source's*, and `re` offers no step budget to bound a
  catastrophic match at answer time — so a pattern that slips through hangs answer-time
  evaluation with the gate neither open nor closed. Each of these was measured
  exponential against `re.fullmatch` on a 26-character field while the guard accepted
  it: a group opened with modifier syntax (`(?:a|a)+`, `(?i:a|A)+`, `(?P<x>a|a)+`),
  whose prefix the lead comparison read as the first alternative's own text; an
  alternative that is itself a group (`((a)|a)+`), whose recorded lead was the closing
  parenthesis; an alternative opening with an escape (`(\da|1a)+`), which the syntax
  scan omits entirely, so the *second* character was compared as the lead; an
  optionally-quantified lead (`(b?a|a)+`, 4655 ms), where `b?` can match empty so both
  branches really begin with `a` and the written first token was never the lead at all;
  and an alternation wrapped in one redundant group (`((a|a))+`, 3017 ms), which the
  boundary scan missed because it looked only at the repeated group's own top level.

  The scanner is now the single place that parses regex structure: it consumes each
  group's modifier prefix, so no caller filters those characters back out, and it
  carries what it resolved — where the body starts, whether the group is atomic,
  whether IGNORECASE is in force — on the token itself. A second function walking the
  pattern its own way is how this guard has gone wrong twice. The alternation check is
  depth-agnostic, matching the repeat check beside it: every group inside a repeated
  one is examined, because ambiguity nested a level deeper is the same ambiguity. Leads
  fold case only inside an IGNORECASE scope, so `(?i:a|A)+` is refused and a plain
  `(a|A)+` — which `re` keeps apart — is not; a scoped `(?-i:` turns folding back off
  inside a pattern that switched it on globally, as the flag does for `re` itself. An
  atomic group and everything inside it is skipped, which keeps `(?>a|a)+` available:
  the engine never re-enters one on backtracking, and making a group atomic is the
  standard repair for exactly this defect, so refusing the repair alongside the bug
  would leave a pack author nowhere to go. (Atomic groups need Python 3.11; on 3.10
  `re` does not recognize the syntax, so such a pattern is still refused — as an
  invalid expression rather than as a backtracking risk.)

  The guard stays syntactic and conservative, and is not a proof of safety in either
  direction: it still refuses shapes that would have been safe (an optional lead is
  reported unknowable rather than resolved, so `(b?a|c)+` is refused too), and a
  construct nobody has taught it to see would still pass. The shapes named above are
  the ones it is known to catch, not the closure of what can backtrack. Beside the
  per-spelling tests, the suite now asserts the complementary property on what actually
  ships — every pattern the guard *accepts* must match adversarial input quickly —
  because enumerating exponential spellings only ever catches the ones somebody
  thought of.

- Let a recorded review settle the coverage policy it was collected for. A policy
  that needs a person was a `safety` no-ship reason until the review was recorded
  — that part worked — but publication readiness also read the policy's own
  verdict, and answer export re-evaluates policies live, so the verdict stayed
  `manual_review` however the review went. The recorded acceptance cleared the
  safety reason and then the same policy raised `attention` from the coverage
  path, which meant no workspace carrying a manual-review policy could reach
  `ship` at all, and the review could never satisfy the gate it was collected
  for. Readiness now matches each `manual_review` verdict against the question's
  `human_reviews` entries by policy id, so an accepted review settles exactly the
  policy it names. Everything about that stays fail closed: a policy with no
  entry is still an open item, a rejected review settles nothing, accepting one
  policy says nothing about another, and no review settles a policy that returned
  `fail` or `contradicted` — accepting a policy is not licence to publish
  evidence that failed it. Both reviewer topologies already write the entries
  this reads, so `question_resolve.py approve` and per-policy
  `question_resolve.py review --verdict accepted --review-ref …` satisfy it
  identically, and the entries ship in the export as the audit trail for why a
  workspace was allowed to publish.

- Refuse a second orchestration driver instead of quietly queueing behind it.
  The per-session lock at `runs/orchestrations/<id>/.locks/session.lock` has
  always covered `start`, `next`, and `submit`, but a contended call waited up
  to ten seconds and then simply proceeded — so two host replicas whose calls
  each finished inside that window both succeeded, serially and silently, and
  the damage surfaced later as inconsistent session state rather than as a
  refused call. Contention is now a loud, machine-readable refusal:
  `ORCHESTRATION_DRIVER_BUSY`, `recoverable: true`, with a new process exit code
  `6` beside the existing `0` ok, `2` invalid, `3` blocked, and `4` paused — `5`
  was left alone because `evidence-wiki orchestrate run`/`resume` already returns
  it for a failed managed runner, and the one status a caller must retry must not
  collide with one it must never retry — and
  a `details.holder` block naming the holder's `agent_id` — `null` when that
  command supplied none — `pid`, `hostname`, `command`, and `acquired_at`. The
  refused call writes nothing: no session document, no appended event, so the
  loser retries against exactly the state it observed. `LOCK_UNAVAILABLE` keeps
  its older, narrower meaning — no lock backend could be established on this
  filesystem at all — which is what finally lets a host tell "another driver
  owns this session, retry" from "this filesystem cannot lock, retrying will
  never help". The library reports the same status a shell would: because no
  error envelope carries an exit status, `exit_code` is reconstructed from
  `error_code`, so this is the first refusal needing an entry in
  `errors._EXIT_CODE_OVERRIDES` — without it the typed exception would have said
  `2` while the process exited `6`, breaking the documented promise that the
  attribute is the status the CLI would have exited with.

  This changes default behavior, and that is the point. A single-driver host
  sees nothing new, because its lock is never contended; a host that was
  accidentally interleaving drivers starts getting `ORCHESTRATION_DRIVER_BUSY`,
  which is the bug surfacing, not a regression. The compatibility lever is
  `--driver-wait-seconds SECONDS` on `start`, `next`, and `submit`: it waits up
  to SECONDS for the holder to release before refusing, restoring the old
  queueing for a host that genuinely wants it. The default is `0`, refuse
  immediately, because the bounded wait is precisely what made interleaved
  drivers invisible. Negative, infinite, and non-numeric values are rejected at
  the CLI boundary rather than clamped.

  The library has the same lever: `driver_wait_seconds=` on
  `ws.orchestrate.start`, `.next`, and `.submit`. Without it "the host decides
  whether to wait or fail" was true only for shell callers, since the facade
  passed no wait and every embedding host took the immediate refusal whether it
  wanted one or not. Omitted, the argument is left out of the controller's argv
  entirely, so the deployed controller applies its own default and this call
  stays byte-identical to the one earlier releases made. An embedding host that
  already serializes its own callers should keep doing that — an in-process
  waiter costs a lock, a controller-side one costs a blocked subprocess — and
  reach for the parameter when the competing driver is in another process,
  which is the case a host lock cannot see.

  `status` remains lock-free and never blocks, so polling a session another
  driver is mutating still returns. The managed-host window lock
  (`.locks/managed-host.lock`, `ORCHESTRATION_ALREADY_RUNNING`) is unchanged and
  complementary rather than redundant: it is host-layer and spans a whole
  managed run or resume, while the driver lock is controller-layer and spans one
  call. Two `start` calls racing on the same explicit `--orchestration-id` may
  surface either `ORCHESTRATION_EXISTS` or `ORCHESTRATION_DRIVER_BUSY` depending
  on how close the race was; both are final and carry the same remediation, so
  callers must branch on the pair. On a filesystem with no native advisory lock,
  where the shared helper falls back to an exclusive-create lock file, a driver
  killed mid-call can block its successors for about two minutes before the lock
  is treated as abandoned; that latency belongs to the fallback backend alone
  and never applies where `fcntl` or `msvcrt` is available.

  Interleaving across the `next` → `submit` *span* is not covered and stays
  deferred to a possible CR-8.1. A per-invocation lock cannot see two drivers
  taking turns between calls, and detecting that needs a session lease or
  fencing token every host would have to carry and return — a protocol change
  this release deliberately does not make. What ships instead is visibility: the
  `action_issued` and `action_completed` events now carry a `driver` block
  (`pid`, `hostname`, and `agent_id` when supplied) in their `data`, so an
  interleaving that slips past the per-invocation lock is legible afterwards in
  `events.jsonl` instead of reconstructed from timestamps. Nothing reads that
  block back to authorize a write.

- Let a domain pack decide its own evidence policies instead of sending every one
  of them to a human. A pack could always name a policy in its own namespace —
  `pack:market-data/quote-48h` — but definition text was the only thing it could
  attach, so every namespaced source, freshness, and identity policy evaluated to
  `manual_review`, including the ones that are pure computation. "A supplier quote
  must be at most 48 hours old" is a subtraction, and a review queue holding
  subtractions is a queue nobody drains. `domain_pack.policy_rules` is the missing
  half: beside the definition a reviewer reads, a pack now declares how this
  package decides the policy unaided. A rule is data, never code — no expression
  language, no callable, no import hook — because a pack that could execute would
  make "what does this workspace do?" unanswerable from the pack's own text. The
  primitive set is closed: `max_age`, `equals`, `numeric_range`, `regex`, and
  `one_of_provenance`, composed by `all_of` and `any_of` at most three deep. A
  primitive that names a `field` addresses it through the same RFC 6901 pointers
  anchor-form grounding already resolves, rooted at `record/` for the source's
  hash-bound structured-view sidecar, `provenance/` for its merged delivery
  provenance, and a bare `question_field: metadata/...` for the question's
  frontmatter — one addressing scheme, three consumers.
  `regex` is a full match and never a search, with the
  pattern capped at 512 characters; `equals` is canonical scalar equality,
  decimal-aware and text-normalized, never containment; `max_age` tolerates five
  minutes of clock skew on a future timestamp and no more, and that bound is
  deliberately not pack-configurable, since a pack able to widen it would be
  loosening a fail-closed bound from inside the thing being bounded.

  Evaluation is fail-closed everywhere. A structured view that is missing or
  corrupt, a pointer that resolves to nothing, a target that is a mapping rather
  than a scalar, a timestamp `max_age` cannot parse — each is a `fail`, never a
  `manual_review`, because degrading to review under adverse conditions would
  quietly recreate the queue this exists to drain, and would do it on exactly the
  sources least deserving the benefit of the doubt. Per-source reasons carry stable
  new prefixes — `rule_field_unresolved`, `rule_value_mismatch`,
  `rule_out_of_range`, `rule_stale`, `rule_future_timestamp`, `rule_regex_mismatch`,
  and `rule_provenance_not_allowed` — beside the existing `structured_view_missing`
  and `structured_view_corrupt`, which a rule reuses rather than restates so a host
  that already handles one handles the other. A malformed declaration fails louder
  still: `evidence-wiki pack validate` reports a new `policy_rules` check failure
  before the pack can ship, and at answer time evaluation refuses with
  `CONFIG_INVALID` instead of treating the pack as declaring nothing — silently
  dropping a pack's automation would return all of its policies to manual review
  without saying so. `manual_review_required: true` keeps the human step in
  addition to the mechanical checks rather than instead of them: primitives fail
  and the verdict is `fail`; they pass and the verdict is `manual_review`.

  Nothing else moves. Verdict rollup is unchanged — a `fail` blocks a required
  facet as any failing policy does, a `manual_review` still routes
  `question_resolve.py answer --require-coverage` to `human_review` — so
  `question_resolve.py` and `publication_readiness.py` needed no changes and shift
  behaviour only because verdicts do. A pack that declares no rules behaves exactly
  as it did, `policy_vocabulary_definitions` keeps its existing contract shape, and
  `evidence_paths` policies stay ruleless because an evidence path names which
  facet must be covered rather than anything a rule could read. What is new on the
  surface: `evidence-wiki contract` gains an additive top-level `policy_rules` key
  reporting each installed pack's declared primitives and review flag; the
  autonomous-required-facets lint stops counting a rule-backed policy as
  manual-only unless it sets `manual_review_required`, since it now *is* a
  deterministic policy, so a required facet may use one without
  `domain_pack.human_gated: true`; and `one_of_provenance` matches providers
  against `provider_registration.id`, `academic_provider`, and
  `standards.registry_provider`, never against `retrieved_by`, which names the
  fetching agent rather than where the evidence came from. Judgement stays where it
  belongs: `pack:general-science/study-recency` — recent enough *for the scientific
  question* — ships with no rule, and should not get one.

- Drive a workspace from Python without spawning a process per operation. Every
  interaction previously cost a `subprocess` spawn plus JSON-envelope parsing,
  including hot-path ones — `orchestrate next`/`submit`, coverage evaluation,
  quote verification — which is the wrong shape for a host that embeds this
  package inside a long-lived ASGI service. `evidence_wiki.Workspace` is the
  embeddable handle:

  ```python
  from evidence_wiki import Workspace

  with Workspace.open("/path/to/workspace") as ws:
      report = ws.coverage.evaluate("electrolyte-conductivity")
  ```

  The subprocess boundary is not removed and not deprecated; the CLI remains the
  supported way to drive a workspace from a shell. What is new is the *choice*.
  Twenty-six operations hang off the handle in namespaces — `ws.coverage`,
  `ws.grounding`, `ws.normalize`, `ws.questions`, `ws.orchestrate` — plus
  `evidence_wiki.fleet_status` and `evidence_wiki.contract`, which no single
  handle owns. Each returns exactly the document the matching `--format json`
  command prints, and `evidence_wiki.contract()["library_api"]["surface"]`
  publishes the list so a host negotiates against it rather than hard-coding a
  version comparison.

  The two doors cannot disagree, because each operation has one implementation.
  Every workspace script grew a `run_<op>(...) -> dict` seam; the CLI prints what
  it returns or renders the refusal's envelope, and the API returns the same dict
  or raises the typed exception built from that same envelope.
  `tests/test_seam_conformance.py` runs the CLI as a real subprocess against the
  seam over identical inputs and requires agreement on the success document, the
  refusal envelope, and the exit code, for every enrolled script.

  Refusals arrive as `evidence_wiki.errors.EvidenceWikiError` carrying
  `error_code`, `message`, `recoverable`, `remediation`, `details`, and
  `exit_code`, sorted into thirteen families by code prefix so a host can catch
  `CoverageError` or dispatch on the exact code. A code this version has never
  seen degrades to the base class with the code preserved, so a newer workspace
  does not break an older host — which is also why `except EvidenceWikiError`
  belongs as the outermost arm.

  Two design decisions are deliberate and load-bearing. Read, evaluate, and
  resolve operations run the *packaged* scripts in-process, so the installed
  library version is the behavior version, exactly as for `evidence-wiki status`
  today. Orchestration `start`/`next`/`submit`/`status` keeps a subprocess to
  `<workspace>/scripts/orchestration_controller.py`, because that controller is
  version-matched to the session state it owns and an in-process shortcut would
  let one library version mutate run state belonging to a different workspace
  version — the same reason managed orchestration is absent from the MCP server.
  Relatedly, `Workspace.open` has no version gate: adding one would make the API
  refuse workspaces the CLI serves. Skew surfaces as the scripts' own typed
  refusals, and `ws.versions()` and `ws.doctor()` are the visibility counterparts.

  Concurrency is as safe as concurrent CLI processes and no safer: the filesystem
  arbitrates through lock files, contention surfaces as `CLAIM_HELD` or
  `LOCK_UNAVAILABLE` rather than corruption, and the API introduces no
  process-global mutation — notably it never redirects `sys.stdout`, which is
  what makes it usable from a multithreaded server at all. Packaged assets are
  held by one process-wide shared root so N handles do not cost N asset
  extractions; `Workspace.close()` invalidates its own handle and leaves that
  root alone. Serializing `next`/`submit` per session remains the host's job.

  Some CLI options have no counterpart on purpose. `--format` and `--output`
  choose a rendering and a destination, not a document; `status --append-log`
  appends to `log.md` after the document is produced; `doctor`'s `env` injection
  point names a type defined in a packaged script asset that a host cannot
  construct. Programmatic `init` and `upgrade` are out of scope — `Workspace.open`
  validates and never creates. The package ships no HTTP server and will not;
  hosts build their own on this API. See `docs/library-api.md`.

- Add a source family the package has never heard of, without forking it.
  Provider ids were validated against closed tuples — `arxiv`, `openalex`,
  `github`, `web` for acquisition — so an embedder whose evidence is market
  data, marketplace listings, or instrument output had exactly one route to it:
  edit the starter scripts, and inherit the maintenance of every transport,
  provenance, budget, and quarantine guarantee those scripts carry. A
  pip-installed distribution can now **register** a provider through an entry
  point, behind a capability declaration the package machine-checks:

  ```toml
  [project.entry-points."evidence_wiki.acquisition_providers"]
  keepa = "autoseller_connectors.evidence_wiki:KeepaProvider"
  ```

  ```python
  class KeepaProvider(AcquisitionProvider):
      id = "keepa"
      provider_api_version = 1
      capabilities = ProviderCapabilities(
          allowed_domains=("api.keepa.com",),
          terms_urls=("https://keepa.com/#!api",),
          license_inference="none",
          rate_limit=RateLimit(60, per="minute"),
          credentials=("KEEPA_API_KEY",),      # env-var NAMES, never values
      )
  ```

  Authorize `keepa` in `research.yml` and fetch through one generic subcommand
  — `fetch_sources.py registered get --id keepa --request-file request.json`,
  with `discover_sources.py registered search` as the read-only mirror.

  What makes the declaration worth more than a comment is a **planner/executor
  split**. The provider is a request planner and a response interpreter: it
  returns `PlannedRequest` values and later turns responses into a
  `SourceArtifact`, and the *package* performs every fetch, through the same
  pinned transport the built-in adapters use — `allowed_domains` checked before
  any socket work, DNS pinning, public-address enforcement, bounded downloads,
  redacted diagnostics — and the package's own writer produces the sidecar and
  the bytes, in that order, with the quarantine discipline built-in acquisition
  already has. The alternative designs were both rejected for the same reason:
  a plugin that owns its sockets, in-process or in a subprocess, turns
  `allowed_domains` into documentation, which is the failure this change exists
  to avoid. Credentials follow from the same split — a provider declares
  variable *names*, writes `{{credential:KEEPA_API_KEY}}` in a header value, and
  the package resolves it at execution and registers the value for redaction.
  Placeholders are refused in URLs, because a secret in a query string would
  survive URL redaction and surface in logs and in `origin_url`.

  Enforcement is stated per field rather than implied. `allowed_domains` and
  `credentials` are enforced at transport, a declared `rate_limit` is enforced
  against a durable per-run ledger that tightens but never loosens
  `max_downloads_per_run`, `captures_raw` and `quarantine_on_incomplete` are
  validated once at registration (v1 requires both `true`, since the writer
  provides both unconditionally and would not honor a `false`), and
  `terms_urls`, `license_inference`, and `request_kinds` are recorded and shown
  by doctor. Recording is not nothing: every artifact acquired this way carries
  `provider_registration` (id, phase, distribution, version, entry point, api
  version) and `provider_capabilities` beside its usual provenance, so an
  auditor can ask which installed code produced a record and what it claimed it
  could reach. `evidence-wiki doctor` gains a Registered providers section
  listing each valid registration with that summary and whether `research.yml`
  authorizes it, plus every invalid registration with the reason it was refused
  — a broken entry point never crashes enumeration and never silently vanishes.
  Six stable codes join the set: `PROVIDER_NOT_REGISTERED`,
  `PROVIDER_REGISTRATION_INVALID`, `PROVIDER_REQUEST_INVALID`,
  `PROVIDER_PLAN_INVALID`, `ACQUISITION_DOMAIN_NOT_DECLARED`, and
  `ACQUISITION_PROVIDER_RATE_LIMITED`.

  One claim is deliberately not made. Installing a Python distribution is
  arbitrary code execution, so a malicious plugin can always open its own
  sockets in-process; nothing here sandboxes third-party code. The enforceable
  guarantee is narrower and is the one the docs state: every fetch performed
  *through the package's acquisition flow* is executed by the package's
  transport and checked against the declaration, and nothing enters the
  workspace except through the package's writer.

  **The authorization boundary is untouched.** Registration makes a provider
  *available*; `research.yml` still *enables* it, by explicit id, and an
  installed-but-unlisted provider refuses with the byte-identical
  `ACQUISITION_PROVIDER_DISABLED` envelope a disabled built-in produces. The
  inverse — an id authorized in an environment where the distribution is not
  installed — is a smoke failure naming the missing distribution, not a warning,
  because an authorization the environment cannot satisfy is drift on an
  authorization boundary; lint deliberately gains no check, so a lint report
  stays a pure function of the workspace tree. Built-in providers, their
  subcommand trees, and every existing error envelope are unchanged, and with no
  entry points installed the built-in lists are the whole universe, bit for bit
  as before. The new
  [provider-registration.md](workspace-template/docs/provider-registration.md)
  documents the authoring contract, the trust boundary, credential custody, the
  failure modes, and the v1 limits — no request signing or OAuth refresh, no
  subprocess isolation, `request_kinds` recorded but not routed, and no
  registering a provider from configuration.

- Ground a claim in a named field instead of a quoted line, and give hosts a
  supported way to write grounding at all. Grounding was one shape — a verbatim
  quote, checked by substring containment against a normalized record's Markdown
  body. For prose that is the right check. For structured evidence it proves the
  record exists and contains *a* sentence, not that the claim's value is in it, so
  the path of least resistance was quoting any line of the cited section and
  calling the answer grounded. A grounding entry now carries `claim`, `source_id`,
  and **exactly one** form of evidence — the existing `quote` (with optional
  `location_hint`), or a new `anchor`:

  ```yaml
  grounding:
    - claim: "Current supplier price is 23.99 EUR"
      source_id: data--keepa--b0abc123
      anchor:
        pointer: "supplier_quote/price"
        expected: "23.99 EUR"
  ```

  `pointer` is an RFC 6901 JSON Pointer with the leading `/` optional, resolved
  against the cited record's structured view, and `expected` is compared to the
  field it reaches by canonical **equality — never containment**. Strings fold
  through the codebase's one deterministic text normalization; numbers compare as
  `Decimal`, so `23.99` and `"23.990"` agree while `"23.99 EUR"` against the number
  `23.99` is an honest mismatch; booleans are checked before numbers, so `true`
  never canonicalizes to `1`. A pointer that reaches a mapping or an array is
  refused (`anchor_target_not_scalar`) rather than searched: an anchor cites a
  field, not a subtree. Query languages were rejected for the same reason — an
  anchor must be a *reference*, and a query that selects a matching value is
  exactly the coincidental match the form exists to remove.

  Anchors resolve against a **structured-view sidecar**,
  `sources/normalized/<safe-source-id>.structured.json`: one JSON object holding the
  complete, uncapped structured rendering of a source, written beside the record and
  bound to it by `structured_view: {path, content_hash}` with a
  `sha256:<64 hex>` digest over the sidecar bytes. The hash is what makes a cited
  value the record's own rather than a neighbouring file's; bytes that no longer
  match are `structured_view_corrupt` and refuse rather than read. Resolving there,
  rather than against the raw payload, keeps pointers facet-shaped instead of
  provider-payload-shaped and keeps the verifier format-free — it always reads
  exactly one JSON object, whatever the source kind was. The sidecar is written
  before the record that binds it, each through an atomic replace, so the worst
  outcome of an interrupted write is an undeclared sidecar (inert, reported by the
  contract check and by lint) rather than a record pointing at a file that never
  existed. This is the fix the rendering-caps work named: `rendered_coverage` still
  describes the capped body a quote is found in, and the sidecar is its uncapped
  complement — a facet capped in the body is reachable verbatim through the sidecar.
  Three producers write one: a normalizer adapter returning the new optional
  `structured` key, the package's own CSV/TSV path, and any external normalizer that
  writes the pair itself and passes `normalize_verify.py`, exactly as CR-2
  legitimized foreign records.

  Native tabular records emit the sidecar from the parse `normalize_sources.py`
  already runs, which previously threw away everything but 20 sample rows with
  80-character-ellipsized cells: `{"columns": [...], "rows": [{<column>: <cell>}]}`,
  header-keyed so a pointer reads `rows/41/price`, every value a string with no type
  inference (`"007"` stays `"007"`), cells verbatim and uncapped. Emission is
  **fail-closed**: nothing is written if the read stopped at the 5 MB ceiling, if any
  row is ragged, or if a header cell is empty or duplicated, and a `parse_warnings`
  entry names which. One shape or none — never a partial one, and never a
  synthesized `price_2` for a duplicated column, which would address a column the
  source never had. Anything excluded degrades to exactly the previous behavior, so
  the addition cannot weaken a gate. Making the package the second producer of the
  sidecar it asks others to emit is the cheapest proof that the contract is not
  adapter-shaped.

  Grounding also stopped being something a host had to hand-edit. `question_resolve.py
  grounding set --slug S --from-file F --agent-id A` replaces a question's whole
  `grounding` block from a YAML (or JSON) file, under the same claim rules and the
  same per-question lock every other mutation uses, in a canonical serialization the
  docs publish as normative — fixed key order, fixed indentation, free-text fields
  always JSON double-quoted so `expected: "23.99"` cannot reload as a float. It
  **replaces** and never merges: grounding is authored as a set for one answer, and
  merging two invites duplicate claims in an order nobody chose. Replacement drops
  any `verified_by`/`grounding_verified_at` stamp in the same write, because the
  entries those attested no longer exist. `grounding: []` clears grounding
  explicitly; `grounding:` with nothing after it is refused as an unfinished edit.
  It deliberately does **not** verify — `verify_quotes.py --slug S` already *is* that
  seam, and a `--verify` flag would mean every future change to verification
  semantics had to remember two doors — so its envelope reports
  `verification: not_performed` and names the follow-up command. `answer
  --grounding-file F` is the single-write alternative: the file's entries and the
  resolution fields land in one atomic write under one lock, and with
  `--require-grounding` it is the *file's* entries that must verify, not whatever the
  page still holds from a previous cycle. A separate `grounding.py` script was
  rejected — it would duplicate claim enforcement, lock and atomic-write plumbing,
  and the frontmatter renderer, when question mutation already lives in exactly one
  script.

  Two new fatal codes join the stable set: `GROUNDING_ANCHOR_INVALID` when at least
  one failed entry is anchor-form, and `GROUNDING_FILE_INVALID` for a grounding file
  that is unreadable, not YAML, or neither accepted shape.
  `GROUNDING_QUOTE_INVALID` keeps its exact old meaning — an all-quote-form failure
  set — because hosts switch on it; both codes carry the full per-entry failure list,
  so a mixed set is fully enumerated whichever one tops the envelope. Verification
  reports gain five per-entry results (`structured_view_missing`,
  `structured_view_corrupt`, `anchor_pointer_not_found`, `anchor_target_not_scalar`,
  `anchor_value_mismatch`), a `form` and `policy` on every entry, and `by_form`
  counts on each question and the report. `normalize_verify.py` reports a
  `structured_view` summary per record and a `with_structured_view` count;
  `NORMALIZED_CONTRACT_STRUCTURED_VIEW_INVALID` names every way a record and its
  sidecar can disagree, including a sidecar sitting beside a record that fails to
  declare it. Lint counts the migration with `grounding_entries_quote` and
  `grounding_entries_anchor` and a `- grounding: quote=N anchor=M` log line, and its
  existing LOW `normalized_orphan` finding now also fires for a sidecar with no
  record beside it — the `normalized_orphans` stat still counts orphaned records
  only, so a host reading that number sees no change. Publication readiness renames
  the grounding reason policy to
  `retained_quote_evidence_or_structured_anchor_evidence`; both original names remain
  verbatim substrings, so a host grepping for either still matches.

  **Backward compatibility is the load-bearing part.** Quote-form grounding is
  verified exactly as before, bit for bit, including its error code and per-entry
  results. There is no new configuration anywhere — the feature is artifact-driven,
  and a workspace whose sources emit no sidecars and whose questions carry no anchor
  entries behaves exactly as it did before. Records that predate the field are
  re-normalized once, and only those whose extraction method could carry a sidecar at
  all: raising `NORMALIZER_VERSION` would have rewritten every record in the
  workspace to reach the few that can hold one. Entailment stays out of scope —
  anchors prove "this exact value in this exact record", deterministically; whether
  the claim *follows* from the value remains a human and policy question. See
  `docs/question-api.md` and `docs/normalized-source-format.md`.
- Let a domain pack declare its own source-request kinds instead of being
  limited to the package's built-in set. A pack overlay lists kind
  declarations under `domain_pack.request_kinds` — a list of mappings with
  required `id`, `label`, and `description` — namespaced exactly like pack
  evidence policies: `pack:<pack-name>/<kind-id>`, with the namespace segment
  required to equal the pack's own `domain_pack.name`, so one pack can never
  declare kinds in another's namespace. Built-in ids stay reserved and
  unprefixed and cannot be redeclared. `evidence-wiki pack validate` checks id
  shape, namespace, and uniqueness before a pack ships; `source_requests.py
  add --kind` resolves the same declaration from the workspace's merged
  `research.yml` and refuses a malformed id with `REQUEST_KIND_INVALID`
  (naming the correctly prefixed id when a bare `<pack>/<kind>` id is
  submitted) or a well-formed but undeclared id with `REQUEST_KIND_UNDECLARED`.
  Everything here is additive: a workspace with no installed pack, or a pack
  that declares no `request_kinds`, accepts exactly the built-in kinds it
  always did.
- Add `structured_data` as a built-in source-request kind — the generic
  non-documentary bucket for evidence like price series, sensor output, and
  other data that isn't a paper, dataset file, web page, or code artifact. It
  dovetails with the existing manifest-side `structured_data` classification
  and CR-2's `normalization.adapters` config: a request opened with `--kind
  structured_data` and fulfilled by a delivery an adapter normalizes closes
  the same loop `paper` or `dataset` requests do. Like every kind other than
  `paper` in the heuristic route planner, `plan-fetch` reports the existing
  manual-delivery warning for it when no candidate is selected; a selected
  discovery candidate routes exactly as it would for any other kind.
- Let a source request carry a structured `scope` — an open key-value mapping
  stating what would satisfy it — instead of relying on a free-text
  `query_or_identifier` and positional convention to pair a delivered source
  back to the request it answers. `add --scope key=value` (repeatable) records
  the mapping on the request; a deliverer states the same keys in the
  `.provenance.yml` sidecar's new `scope:` field. `fulfill` always checks for
  contradiction — a key both sides declare with different values refuses with
  `REQUEST_SCOPE_MISMATCH`, naming the conflicting keys and both values, and
  writes nothing — which is what stops a source that answers facet Y from
  being recorded as fulfilling a request for facet X. `fulfill --match-scope
  key=value` lets the caller assert scope keys when fulfilling, checked the
  same way. `fulfill --require-scope` upgrades absence to a refusal: every key
  the request declares, and every key `--match-scope` asserts, must be present
  and equal in the delivered scope, or the fulfilment is refused with
  `REQUEST_SCOPE_MISSING` — the fail-closed mode for a delivery pipeline that
  can guarantee it stamps scope. Malformed
  `--scope`/`--match-scope` input (missing `=`, a malformed key, a repeated
  key) is refused with `REQUEST_SCOPE_INVALID`. None of this is required: a
  request that declares no scope, or a delivery whose sidecar declares none,
  behaves exactly as before — the contradiction check can only fire when both
  sides opted in.
- Pair a `reopen`'s supplied requests and sources by declared scope instead of
  by argument order. `question_resolve.py reopen` now computes, for each
  scoped request among the supplied `--request-id`/`--source-id` pairs, which
  supplied source's provenance scope does not contradict it, and reports the
  pairing as `pairs` in its result. A scoped request that matches no supplied
  source, or an assignment that would pair one source with two scoped
  requests, refuses with `REQUEST_SCOPE_MISMATCH` before any write. Requests
  and sources without scope fall back to the previous unpaired behavior, so an
  unscoped `reopen` call is unchanged.
- Back-fill a request's scope from the coverage manifest that blocks on it.
  `coverage_manifest.py set-facet --blocking-request-id` now writes the linked
  facet's id into the request's `scope.facet_id` when the request doesn't
  already declare one, so the manifest and the request can never disagree
  about which facet a request unblocks. A request that already carries a
  different `scope.facet_id` refuses the facet write with
  `FACET_SCOPE_CONFLICT`, naming both facet ids and the request. The
  back-fill is idempotent — rerunning `set-facet` is a no-op — and
  `--clear-blocking-request-ids` does not clear the request's own scope, since
  the request still states what would satisfy it even after the manifest link
  is removed.
- Add `kind` and `scope` read filters to the request-listing surfaces.
  `source_requests.py list --kind <id>` (repeatable) and `--scope key=value`
  (repeatable, AND semantics, exact string equality) narrow the listing the
  same way `--status` already does; a malformed `--scope` pair is refused with
  `REQUEST_SCOPE_INVALID` and an unknown `--kind` with
  `REQUEST_KIND_UNDECLARED`. The MCP `source_requests_list` tool gains
  matching `kind` and `scope` arguments so a host reading through MCP —
  `serve-mcp` is the read surface for every other service — can filter the
  same way. Both filters are optional additions to an existing read path;
  omitting them returns exactly what `list`/`source_requests_list` returned
  before.
- Let a registered external acquirer fulfil source requests, so a host that
  already owns fetching infrastructure — rate limits, credential custody, egress
  policy, compliance logging — can supply evidence without the workspace fetching
  anything. Previously such a host had no sanctioned path: with
  `integrations.acquisition.providers: []` the controller had no provider to
  address acquisition work to, so the phase was unreachable and the only way to
  close a blocked question was to run `source_requests.py fulfill` out of band,
  after `submit` returned. That produced exactly the thing the integrity baseline
  exists to prevent — a mutation of durable evidence state that no work order
  accounts for. `research.yml` can now name the acquirer instead:

  ```yaml
  orchestration:
    acquisition: delegated
    acquirer_agent_id: autoseller-orchestrator
  ```

  Under that declaration the controller issues acquisition work orders scoped to
  the open requests and addressed to `assigned_agent_id`, and the acquirer does
  its delivery, fulfilment, and question reopening *inside* the pending order.
  Submission verifies the result the same way it verifies a provider acquisition:
  every scoped request must end with a fulfilment whose evidence is normalized and
  linked by a provenance sidecar's `request_id`, or with a structured failure in a
  new append-only attempt audit naming that action. A partial batch is `completed`;
  `blocked` means nothing durable changed. While a delegated session is live, the
  workspace commands refuse mutations no pending order scopes
  (`SOURCE_REQUEST_FULFILL_DELEGATED`, `QUESTION_REOPEN_DELEGATED`), which is what
  makes "fulfilment stays inside the protocol" enforceable rather than advisory.

  Failed attempts use the shared delivery taxonomy plus three connector-level
  codes (`provider_throttled`, `not_authorized`, `no_result`); a request is retired
  after `max_attempts_per_request` attempts within a session, or immediately on a
  standing refusal, after which the session terminates `blocked_on_sources` with a
  stable reason prefix and an `exhausted_requests` map. Attempts are counted per
  session, so starting a new one is the supported retry after a host-side fix.

  Everything is opt-in and additive: `acquisition: providers` remains the default
  and is unchanged, the session and work-order schemas stay at `1.0` with optional
  fields, and managed `orchestrate run`/`resume` refuse a delegated workspace up
  front, since a delegated order is addressed to the host's own connectors. See
  `docs/orchestration.md`, `skills/research-acquire-delegated.md`, and the
  `orchestration:` section of `docs/research-yml.md`.

- Promote `docs/normalized-source-format.md` to a versioned public contract so an
  external normalizer can produce first-class normalized records for source kinds
  this package does not extract itself. Every record now declares the contract
  version it conforms to in `normalized_format` (currently `1`), which is separate
  from `normalizer`: `normalizer` says which tool produced a record and is only a
  regeneration trigger for this package's own output, while `normalized_format` is
  the single field an external writer must track. `evidence-wiki contract` reports
  the written version, the accepted versions, and the running normalizer identity
  under `normalized_source_format`. A record written outside this package must
  declare `normalized_format`; a record with no such field is read as a legacy
  record from this package's own normalizer. `docs/normalized-source-format.md`
  now carries the external-writer workflow, including which sources an external
  tool may write records for: records for kinds `normalize_sources.py` does not
  read are never touched, while a record written for a kind it does read is
  regenerated by the next normalization run.
- Deliver the commented optional-section examples into generated workspaces.
  `research.yml` is written by dumping parsed YAML, so the starter's comments never
  survived initialization and an operator's own config listed only active keys —
  giving no hint that scoped human review (`review:`) or external normalizer adapters
  (`normalization:`) could be enabled at all. Initialization now appends those
  examples verbatim from the starter as a commented footer, pointing at
  `docs/research-yml.md`. The blocks are extracted rather than duplicated, so the
  starter config stays the single source of truth and a future optional section is
  delivered without code changes; prose annotating active sections is not appended,
  and the footer adds no active configuration. `evidence-wiki upgrade` still never
  rewrites `research.yml`, so a section an operator enabled is untouched.
- Make quotability under rendering caps explicit. A normalized body for structured
  evidence is a rendering with caps, and grounding is verified by containment against
  that body — so whatever a renderer dropped is citable but not quotable, and a record
  that rendered a tenth of its payload looked exactly like one that rendered all of it.
  Records now carry `rendered_coverage`: the values considered, the values rendered,
  the ratio, and per-section counts with a `note` recording each cap. It is **required**
  for records this package renders through an adapter — the protocol can demand it, and
  a renderer that caps nothing reports equal counts with `ratio: 1.0` — and optional
  elsewhere, but checked whenever declared. The package verifies the claim is coherent
  (rendered never exceeds considered, the ratio matches its own counts, section counts
  stay within the record's totals, and a section claiming rendered values names a
  heading the body actually has) but never recomputes the counts, because it cannot
  know a foreign renderer's capping rules — which is why the renderer states them. A
  section that rendered nothing needs no heading: a facet dropped in full has none to
  point at, and its entry exists precisely to account for the loss. An adapter that
  omits or misstates coverage fails the action and writes no record.
- Surface rendering caps where an operator will see them. Every record entry in the
  `normalize_verify.py` report now carries `rendered_coverage` with the declared ratio,
  the two counts, and `capped_sections` naming the facets that lost content — the parts
  a claim can cite but never quote. The field is `null` for a record that declares no
  block, which is not the same as one that rendered nothing, and coverage is reported
  for invalid records too, since triaging one still needs to know how much of it is
  quotable. Lint gains an opt-in companion: set `lint.min_rendered_coverage_ratio` and
  any record declaring a lower ratio emits LOW `normalized_low_rendered_coverage`,
  counted in the `normalized_low_rendered_coverage` stat. It is unset by default and
  LOW when it fires, because capping a long series is a legitimate rendering choice
  rather than a defect — this is a visibility tool, and CR-7's structured grounding
  anchors are the actual fix for un-quotable content. Unlike the contract check, it
  applies to native and foreign records alike; a threshold that is not a number in
  `[0, 1]` disables the notice instead of failing the run.
- Cover the structured-evidence path end to end.
  `tests/test_structured_evidence_e2e.py` walks a delivered JSON payload and its
  sidecar through the whole chain in a workspace built by initialization — inventory,
  adapter normalization, contract verification, reopening the blocked question,
  grounding a claim in a facet value, lint, and the orchestration controller's own
  workspace-safety postcondition. The legs are load-bearing in sequence rather than
  individually: normalization is what opens the reopen gate, and the facet headings the
  adapter emits are what make a value quotable at all, so a regression in any stage
  surfaces as a broken chain instead of a passing unit test about a stage nothing can
  reach. Two companion cases pin the boundaries — the same delivery with no adapter
  configured is classified but never normalized and the gate stays shut, and a capped
  rendering still grounds what it did render while a dropped facet fails closed with
  `anchor_not_found`.
- Hold the hand-written path to the same chain. The same suite now runs a record an
  external tool wrote itself, with no adapter configured anywhere: it verifies against
  the same validator, opens the reopen gate, grounds a quote from a facet section, and
  leaves lint with nothing to say beyond the missing wiki source note — the same residue
  the adapter path leaves. A full `--all` normalization run is asserted to leave that
  record byte-identical, since a kind nothing here normalizes belongs to whoever wrote
  it. One mutation per contract violation family then shows each is named: verify
  reports exactly that family's code and nothing else, and lint reports it as MEDIUM
  `normalized_record_contract_violation` with the same code. The one asymmetry is
  asserted rather than glossed — a record with no frontmatter names no producing tool,
  so lint cannot tell it from a stray Markdown file and leaves it alone, while
  `normalize_verify.py` still refuses it.
- Guard the "no adapters configured, no change in behavior" promise.
  `tests/test_no_adapter_backward_compat.py` runs an existing fixture twice with one
  variable changed — whether `research.yml` has a `normalization:` section at all — and
  compares whole artifacts: the manifest, every normalized record byte for byte, the
  normalization report, the verifier report, and the lint payload. The declared command
  points at a path that cannot exist, so an adapter consulted for a kind it does not map
  would surface as a failed action rather than a silent no-op. Three further cases cover
  what the differential cannot see: a second normalization run rewrites nothing (the
  bumped `NORMALIZER_VERSION` must not perma-stale the records it was added to); a
  record written before the contract existed is still accepted, uncounted as foreign and
  unflagged; and the native frontmatter shape is pinned key by key, so a field added to
  every record has to be declared here rather than appearing quietly.
- Document the record fields that were being written but not published. Five
  codebase-record fields — `codebase_intake_status`, `codebase_execution_scope`,
  `codebase_artifact_manifest`, `codebase_artifact_checksums`, and
  `codebase_artifact_provenance` — went into every normalized record without appearing
  anywhere in `docs/normalized-source-format.md`, the contract hosts are told to target.
  They now have field-table rows covering the four intake verdicts, the standing
  `external_worker_only` nonexecution boundary, and the fact that
  `trust: self_asserted_external_worker` is literal: the worker asserts how it ran and
  this package records the assertion rather than confirming it. `title_confidence` and
  `rendered_coverage` gained rows too, so every field a record can carry is now in the
  table. One related correction: the HTML section claimed a title fallback produces a
  "lowered `title_confidence`", but that field is written for PDF records only and stays
  `null` on HTML records — the fallback is recorded in `parse_warnings`.
- Close the agent-facing gap for the two new lint findings. `research-lint` now covers
  `normalized_record_contract_violation` (run `normalize_verify.py` for the full list of
  breaches, then have the producer re-deliver) and `normalized_low_rendered_coverage` (a
  signal about which facets a claim cannot quote, not a defect to repair), and its fix
  policy now refuses hand-editing any record whose `normalizer.name` is not this
  package's: the edit would make the record claim a conformance its producer never gave
  it, and for a kind nothing here normalizes the edit sticks and hides the producer's
  bug. The example workspace's two legacy link records gained the `evidence_usable`
  field they predated, so the shipped workspace verifies against the contract. The
  format document now also says plainly that the legacy-absent rule exempts a record
  from the version check only — a record written before a required field existed can
  still be reported by `normalize_verify.py`, which the first re-normalization repairs
  and which lint never escalates.
- Make every normalized record in this repository pass its own contract, and keep it
  that way. Four fixture records did not: three battery-workspace stubs that carried no
  `type` at all, and a standards record missing half its required frontmatter and living
  at a path its `source_id` did not resolve to. Fixtures are what a reader opens to learn
  the format, so one that fails the verifier teaches the wrong shape and makes the
  verifier look broken. They are now full records — quoted prose preserved verbatim under
  `Extracted Text`, so grounding still verifies against them. A new conformance test walks
  the repository for workspaces with committed records rather than naming them, so a new
  fixture is enrolled by existing. Nothing caught this before: lint holds only externally
  produced records to the contract, which is what leaves a native record missing a field
  invisible.
- State the machine-output contract for hosts: `docs/orchestrator-handoff.md` gains a
  "Machine Output On stdout" section — under `--format json`, stdout carries exactly
  one JSON document, diagnostics go to stderr, and a fatal error leaves stdout empty —
  cross-linked from `docs/research-yml.md` and `docs/normalized-source-format.md`.
  **Embedders can delete defensive parsing**: there is no need to scan for the first
  `{`, skip banner lines, or tolerate trailing output, because a script producing any
  of those is failing the contract rather than expressing a variation of it. The
  section also names the two things not to mistake for violations — a non-zero exit
  still carries a report from the commands whose job is to assess a workspace, and
  `source_inventory.py --dry-run` without `--report` keeps its documented JSONL stream.
- Enforce machine-output purity per script. `tests/test_json_stdout_purity.py` runs
  every command that accepts `--format json` as a subprocess — in a real workspace and
  against an unreadable one — and asserts stdout parses as exactly one JSON document
  with nothing before or after it, by consuming the whole buffer rather than scanning
  for the first `{`. Fatal paths must leave stdout empty and put the shared envelope on
  stderr. Enrollment is automatic: a script that declares `--format` and is not listed
  fails the suite, so a new command cannot skip the contract. Two surfaces are encoded
  as the documented exceptions they are — `source_inventory.py --dry-run` without
  `--report` keeps its JSONL stream contract, and `query_index.py build-index` accepts
  no `--format` at all.
- Send `scripts/workspace_gc.py` fatal errors to stderr instead of stdout. Under
  `--format json` it printed a hand-rolled error object on stdout, where a caller
  parsing stdout could not tell it from a report document, and the object omitted
  `recoverable` so it did not match the envelope every other script emits. It now
  reports through the shared `emit_error` helper: stdout stays empty and stderr
  carries the standard envelope. This was the only stdout offender found in an audit
  of all 22 scripts that accept `--format json`, covering success paths, fatal-error
  paths, and reports about an unreadable workspace.
- Surface configured normalizer adapters in `scripts/doctor.py` under a new
  `normalization_adapters` check, so the one place normalization runs something the
  package did not ship is visible in the same preflight that reports runtime
  dependencies and workspace permissions. The check lists each adapter's kinds,
  command, declared identity, and timeout, states plainly when none are configured,
  and reports an invalid section as degraded with its `CONFIG_INVALID` code. Doctor
  only reads the declaration and never executes the command.
- Report a malformed `research.yml` `normalization:` section through the shared error
  envelope instead of a traceback. The section is read before any record is selected,
  so it cannot become a per-source failed action; `normalize_sources.py` now exits `2`
  with `error_code: CONFIG_INVALID`, its remediation, and empty stdout, matching every
  other fatal setup error.
- Tell an operator the right fix when a link file's URLs are not inventoried. A `.txt`
  URL list is only expanded into one source per URL when it sits under a link root, so
  a list delivered elsewhere becomes a single `link` record whose URLs never become
  sources. The inventory report surfaced that record but offered one action — "Fix
  malformed raw link files or remove them from raw link roots" — which describes the
  other case: such a file is not malformed, and moving it *out* of a link root is the
  reverse of the remedy. `link` records now carry `metadata.link_parse_status`
  (`outside_link_root` or `no_urls_parsed`), and the report emits the matching action
  for each, so a misplaced list is told to move under a link root or take a
  `.url`/`.webloc` extension. Records written before the field default to the parse
  failure, keeping their previous guidance.
- Report adapter work everywhere normalization reports itself: `summary.methods`
  gains an `adapter` counter in the JSON report, on the stderr summary line, and in
  the `log.md` entry written by `--append-log`, and an action produced by an external
  normalizer carries an `adapter` object naming the tool and version, so a report
  reader can attribute a record without opening it. The normalization report's
  `schema_version` stays `"1.0"`: new counters and action fields are
  forward-compatible additions, and the package bumps a schema version only for
  breaking shape changes.
- Decide adapter-record freshness by the adapter's identity rather than this package's
  normalizer version. An adapter record stores its adapter's version — often a string
  like `"1.4.0"` — which is not comparable to the package's integer
  `NORMALIZER_VERSION`, so every adapter record read as stale and every run
  re-executed the adapter to reproduce a record the workspace already had. A run now
  rebuilds an adapter record only when the configured adapter `name`/`version` differs
  from the producer the record names, or when the source's `raw_fingerprint` changed;
  otherwise it is reported `skipped_existing`. Bumping `version` in `research.yml` is
  the way to force a rebuild after changing an adapter's behavior. Native records are
  unaffected and still use the `NORMALIZER_VERSION` axis.
- Run external normalizer adapters from `normalize_sources.py`, closing the loop for
  evidence this package cannot extract. A record whose kind is mapped in
  `normalization.adapters` is selected, normalized, and re-normalized on staleness
  like any other record; the adapter returns content and the package writes the
  record, so frontmatter, section order, `raw_fingerprint`, and `content_hash` stay
  owned by one writer while `normalizer` names the adapter as producer and
  `extraction_method` is `adapter`. The protocol is one JSON document in on stdin, one
  out on stdout, documented in `docs/normalized-source-format.md`.
  Adapter output is treated as untrusted input and every failure is closed: a non-zero
  exit, a timeout, output that is not exactly one JSON document, an identity that does
  not match what `research.yml` authorized, a `failed` status, a missing body, a `##`
  heading that would collide with the record's own sections, or output past the size
  cap all produce a failed action with a named reason, a non-zero exit code, and no
  record on disk. The package re-checks its own rendering against the record contract
  after writing and removes the file if it does not conform, so a run can never leave
  a non-conforming record behind under the package's own name.
- Classify `.json` and `.jsonl` raw files as the new built-in `structured_data`
  kind, so structured payloads have a name an adapter can be mapped to instead of
  falling under `unknown`. Records carry a `raw_fingerprint` like other
  content-derived kinds, so a record built from a payload goes stale when the
  payload changes. The kind is classified-only: this package does not extract
  structured payloads, so nothing is normalized unless a workspace configures a
  `normalization:` adapter for the kind or an external normalizer writes the record.
  Re-running inventory on an existing workspace relabels affected records in place —
  source ids are path-derived and unchanged, and `detected_at` is carried forward.
  `.jsonl` previously classified as `table`, but only `.csv`/`.tsv` were ever
  normalized as tables, so nothing that worked before stops working. Lint is
  unaffected: `source_missing_normalized` already fired for these records under their
  old kinds and fires identically now.
- Add the optional `research.yml` `normalization:` section, which maps manifest
  source kinds to an external normalizer command so evidence this package cannot
  extract can produce normalized records through the normal pipeline. The section is
  read and validated by `scripts/_normalization_config.py`; when it is absent no
  adapter exists and normalization is unchanged, and
  `compatible_research_yml_contract` stays at `0.1` because the section is optional
  and additive. The template ships it commented out — executing a command is opt-in.
  Because configuring an adapter authorizes this package to run that command, the
  shape is strict: `command` must be an argv list (a string is refused rather than
  split, so argument boundaries are never guessed), `provider: command` must be
  stated explicitly, `name`/`version` are required so a record's claimed producer can
  be checked against what the workspace authorized, `timeout_seconds` defaults to 120
  and is bounded at 3600, one kind resolves to exactly one adapter, and kinds
  `normalize_sources.py` extracts itself are refused so a single config line cannot
  silently divert papers or PDFs from the built-in extractors. Kind membership is
  otherwise open, so a domain pack may declare its own. A malformed section is
  rejected rather than defaulted to "no adapter", which would let a workspace believe
  its structured evidence had been normalized when nothing ran.
- Lint now holds externally produced normalized records to the published record
  contract instead of accepting any file that exists. A record naming a producing
  tool other than this package's is accepted exactly as a native record when it
  conforms, and emits MEDIUM `normalized_record_contract_violation` naming the first
  breach, the offending field, and the remediation when it does not. The finding is
  MEDIUM rather than HIGH deliberately: a HIGH finding stops the orchestration
  controller from issuing the next work order, and one malformed record must not
  freeze research across a workspace — quote verification and the reopen check
  already fail closed on the record itself. Lint stats gain
  `sources_foreign_normalized` and `normalized_contract_violations`. Records that
  name no producing tool are unchanged: they predate the contract rather than claim
  it, and `normalize_verify.py` checks every record regardless. The existing
  `source_missing_normalized` finding for an absent record is untouched.
- Add `scripts/normalize_verify.py` and `evidence-wiki normalize verify`, which check
  normalized records against the published record contract so a host writing records
  with its own normalizer can prove they conform instead of matching an internal
  format by inspection. Records written outside the package are checked on exactly
  the same terms as records the package wrote. Selection is every record under the
  normalized directory, or specific `--source-id` values; `--format json` (default)
  emits one `normalize_verify_report` document naming each record's origin
  (`native` or `external`), declared contract version, verdict, and violations, and
  `--format text` renders the same findings for a human. A contract breach is report
  content rather than a fatal error, so one malformed record never hides the rest:
  the command exits `1` when any record is invalid and `2` only for a workspace it
  cannot read, which is reported through the shared stderr error envelope.
- Decide contract conformance in one place: `scripts/_normalized_contract.py`
  validates a normalized record against the published format and returns stable,
  namespaced violation codes (`NORMALIZED_CONTRACT_FRONTMATTER_MISSING`,
  `…_FRONTMATTER_INVALID`, `…_FORMAT_VERSION_UNSUPPORTED`, `…_SECTIONS_INVALID`,
  `…_MANIFEST_MISMATCH`, `…_WARNINGS_INCONSISTENT`), each carrying the offending
  field, what was expected, and what was found. `evidence-wiki contract` lists the
  codes under `normalized_source_format.violation_codes` so a host can handle them
  without scraping text. The module also owns the contract's own definitions — the
  accepted format versions and the source-id-to-path rule — which
  `normalize_sources.py` now imports rather than duplicating, so the record the
  package writes and the record it validates cannot describe different formats.
- `NORMALIZER_VERSION` moves from 2 to 3 to carry the new field, so the first
  normalization run after upgrading regenerates existing normalized records once to
  backfill `normalized_format` — the same one-time re-normalization that previous
  normalizer bumps performed. Grounded questions citing regenerated records should
  be re-verified with `verify_quotes.py --slug <slug> --write` before the next
  readiness evaluation, as documented for any change to normalized evidence text.

- Scope `human_review` escalation to the question instead of the workspace,
  behind a new optional `research.yml` `review:` section. Under
  `review.escalation_scope: question`, a question awaiting human review no
  longer flips the readiness verdict to `attention_required`: it is reported as
  `readiness.questions_awaiting_review`, orchestration keeps issuing work for
  the other questions, and a workspace whose only remaining work is pending
  reviews reports `in_progress` with the structured code
  `questions_awaiting_review_only` rather than `complete`. The default
  `review.escalation_scope: workspace` preserves 0.2.4 semantics, and
  `compatible_research_yml_contract` is unchanged at `0.1` because the section
  is optional and additive.
- Add `question_resolve.py review --slug S --policy P --verdict
  accepted|rejected --reviewed-by PRINCIPAL [--review-ref REF] [--note TEXT]`,
  which records a human review collected outside the workspace against one
  coverage policy at a time. Entries append to a `human_reviews` frontmatter
  list; the question becomes `answered` once every declared policy is accepted,
  writing the same review fields `approve` has always written, and a rejection
  returns it to `open` with the reason retained. `approve` is unchanged for
  callers and now accepts every still-pending policy through the same writer.
  Publication readiness accepts recorded external reviews exactly as it accepts
  `approve`, and still refuses to ship an answer whose required review is
  unrecorded.
- Report a review queue that has stopped moving: under
  `review.escalation_scope: question`, lint emits HIGH
  `question_human_review_stale` once a question has awaited review longer than
  `review.max_pending_review_hours` (default 168, `null` disables), which
  returns the workspace to `attention_required` through the existing
  lint-to-verdict path. A parked question with no usable
  `human_review_requested_at` emits MEDIUM `question_human_review_undated`.
- Surface the review queue for reviewers and hosts: `workspace_status.py` text
  output and MCP payload carry the awaiting-review count and slugs,
  `question_status.py` records carry `human_review_requested_at` and
  `human_review_pending_policies` (rendered as age and pending-policy count in
  text), and `fleet_status.py` and `run_report.py` report the counter per
  workspace. `export_answers.py` exports the per-policy `human_reviews` entries
  for audit.
- Declare the `human_review` question status and the review frontmatter fields
  in the starter `research.yml` frontmatter rules, so a parked question no
  longer draws a spurious `frontmatter` lint finding in a stock workspace.
- Refactor managed Codex and Claude execution behind a closed adapter registry,
  and document OpenCode, Pi, and other harnesses as external-protocol clients.
- Bump the reusable managed workspace starter to `0.5.5`, including the
  canonical `CLAUDE.md` instruction pointer in its required asset manifest.

## 0.2.4 - 2026-07-21

- Treat broken or partially initialized pypdf import-spec lookups as a missing
  required dependency in workspace health checks, returning a typed finding
  instead of raising an inspection error.
- Restore DNS and HTTPS for provider-enabled managed Codex actions on
  Linux/WSL2, including symlinked `/etc` layouts, by adding bounded read-only
  system resolver configuration to the named permission profile, while keeping
  offline actions unchanged and rejecting unexpected external resolver
  targets.
- Bump the reusable managed workspace starter to `0.5.4`; `evidence-wiki
  upgrade` refreshes its health-check and orchestration guidance while
  preserving project evidence and configuration.

## 0.2.3 - 2026-07-21

- Install pypdf as the portable, canonical PDF normalization backend so a
  normal wheel installation can process PDF-only evidence on macOS, Linux, and
  Windows without an undeclared system executable.
- Treat Poppler `pdftotext` as an explicit optional compatibility backend,
  document the pip/system-package boundary and platform installation commands,
  and align doctor/workspace-health reporting with the actual backend policy.
- Pause and replay retryable blocked orchestration actions, while treating an
  audited candidate-specific `selected` to `failed` acquisition transition as
  one exhausted route so the controller can try the next candidate before it
  declares terminal `blocked_on_sources`.
- Reject acquisition fulfillment backed by failed, stubbed, explicitly
  unusable, or text-empty normalized evidence, and validate the request
  correlation supplied for definitive candidate-route failures.
- Harden Windows workspace locking and managed-run runtime handling so
  concurrent index builders and protected virtual-environment checks fail
  predictably instead of raising platform-specific permission errors.
- Bump the managed workspace starter to `0.5.3`; `evidence-wiki upgrade`
  refreshes the compatible managed scripts while preserving project evidence and
  configuration.

## 0.2.2 - 2026-07-21

- Canonicalize package-managed runner results by discarding descriptive
  `runs/orchestrations/` artifact references before submission, while keeping
  direct protocol validation strict and retaining fail-closed control-tree
  mutation detection.
- Pin managed workspace scripts to the Python interpreter that launched
  EvidenceWiki, disable Codex login-shell PATH rewriting, grant its external
  runtime read-only, and preflight its PyYAML/TLS dependencies inside an
  isolated read-only permission profile before creating a session.

## 0.2.1 - 2026-07-21

- Fix the managed Codex result schema to use the supported Structured Outputs
  subset while retaining strict host-side validation of returned artifacts.
- Fail closed before managed execution when the runner cannot protect the
  host-owned parent orchestration tree. Codex requires its supported 0.138+
  permission-profile interface, and managed Claude execution is unavailable on
  native Windows. Claude isolation requires `bubblewrap` and `socat` on
  Linux/WSL2 or `sandbox-exec` and `touch` on macOS.
- Define the semantic baseline as bounded **tripwire-protected controls**:
  workspace contract/instruction files, `scripts/`, `skills/`, `docs/`, and the
  current parent session. Ignore timestamp-only drift and report exact
  workspace-relative changes without automatically restoring or rolling back
  operator-visible files. Keep `.git/`, `.codex/`, `.claude/`, `.agents/`, and
  workspace virtual environments, plus `runs/orchestration-guards/`,
  preventively read-only without adding those roots to the post-action
  tripwire snapshot.
- Add bounded `orchestration_attempt` records, private staged-result recovery,
  never-submittable quarantined results, and a durable control-repair marker
  without retaining prompts, transcripts, diagnostics, secrets, or absolute
  paths. Retain the guard outside the parent session at
  `runs/orchestration-guards/<orchestration_id>.json`.
  `CONTROL_ARTIFACT_TAMPERED` records the tripwire failure, and
  `CONTROL_REPAIR_REQUIRED` blocks managed resume until explicit review;
  acknowledgement requires the saved tripwire-protected-control snapshot and
  fails with `CONTROL_REPAIR_MISMATCH` when it still differs or
  `CONTROL_REPAIR_BASELINE_MISSING` when no trustworthy baseline survives.
- Make managed recovery checkpoint-first: use an accepted canonical result, an
  identical clean staged result, or then the same persisted action in a fresh
  worker. Deterministic submission and trusted-input fingerprints remain
  authoritative.
- Serialize each managed parent session for its full drive and return
  `ORCHESTRATION_ALREADY_RUNNING` before launching a competing worker. External
  protocol hosts must provide equivalent session-wide coordination.
- Refuse overlapping retained attempts with `ORCHESTRATION_LEASE_ACTIVE`, fail
  malformed or expired absolute leases with `ORCHESTRATION_LEASE_INVALID` or
  `ORCHESTRATION_LEASE_EXPIRED`, and cap each runner timeout to the lease's
  remaining lifetime.
- Strengthen discovery and candidate-review immutability checks with a bounded
  content digest for up to 10,000 raw files / 2 GiB plus the exact record count
  and content digest of `sources/manifest.jsonl` up to 32 MiB.
- Forbid daemons, hooks, background jobs, and detached subprocesses in managed
  work orders. Clean up the runner process group while documenting that
  untrusted process trees require an operator-controlled container or VM.
- Move new generated run reports to `runs/run-reports/`; existing
  `docs/run-reports/` files remain historical read-only inputs.
- Bind legacy pending actions to controller-owned static-input fingerprints on
  their first replay, and recompute authoritative verification/export outputs
  before accepting completion.
- Bump the managed workspace starter to `0.5.2` with explicit parent-control
  ownership and recovery guidance for research, discovery, acquisition, and
  verification workers.
- Make managed Codex runtime visibility exact and portable: resolve the selected
  executable and official platform package outside the writable workspace,
  grant only its canonical runtime tree as read-only, and preserve the same
  permission profile for capability probes and worker actions. Correct the
  empty Claude MCP configuration document without weakening either runner's
  sandbox or approval policy.
- Require every research action to prove scoped question progress, enforce the
  remaining per-run question budget, keep review and acquisition bound to the
  persisted request/candidate IDs, and allow rediscovery after exhausted or
  unroutable candidates without mutating terminal child runs.
- Add durable pre-transport arXiv/OpenAlex request accounting, fail closed on
  corrupt run accounting, and derive status counters from discovery requests
  rather than acquisitions. GitHub and OpenAlex acquisitions now retain both
  request and candidate provenance for deterministic orchestration checks.
- Publish complete Draft 2020-12 schemas for orchestration sessions, work
  orders, results, and managed attempts while keeping the existing schema
  version map stable for compatibility.
- Keep exact pre-action question, request, candidate, manifest, raw, and
  normalized-evidence guards in protected controller sidecars, leaving public
  work orders bounded to 256 KiB. Validate sidecar identity and content before
  replay, reject every out-of-scope mutation, and accept acquisition only when
  fulfilled evidence is either an unchanged scoped match or a genuinely new
  provenance-linked source.
- Roll an exhausted immutable child run into a fresh child for remaining
  research, discovery, or acquisition work, including source-request budget
  exhaustion while actionable questions remain.
- Bump the managed workspace starter to `0.5.2`; the default upgrade refreshes
  managed scripts while preserving existing research configuration, evidence,
  and wiki content. Optional skills and documentation remain reviewable
  `--include` groups and are never replaced silently.
- Fail closed when a pending legacy research, discovery, candidate-review, or
  acquisition action lacks the phase-specific pre-action baseline needed for
  deterministic reconciliation. Preserve the old parent session and start a
  fresh orchestration; never reconstruct these baselines after worker execution.
  Active legacy runs without the new academic-provider accounting marker also
  require a fresh run so an unknown prior call count cannot reset to zero.

## 0.2.0 - 2026-07-20

- Bump the managed workspace starter to `0.5.0` and package the orchestration
  controller and shared provider registry as upgrade-managed scripts.
- Add durable parent orchestration sessions with model-neutral work orders,
  Codex and Claude managed runners, restart-safe status, and verified result
  submission across immutable bounded research runs.
- Add explicit discovery/acquisition provider flags, fail-closed discovery
  provider validation, and request-backed arXiv/OpenAlex academic discovery.
- Treat legacy `legal`, `authors`, and `companions` discovery entries as
  deprecated strategies rather than provider authority; migration is manual
  because upgrades preserve `research.yml`. Enabled discovery with no concrete
  provider is now a HIGH configuration error.
- Document the empty-source autonomous workflow, source-provider permissions,
  runtime credentials, and the local-files-only alternative.

## 0.1.0 - 2026-07-13

Initial standalone release of EvidenceWiki.

- Verifiable, provenance-backed research workspaces with deterministic question,
  source, citation, and publication-readiness workflows.
- The `evidence-wiki` CLI for workspace creation, upgrades, health checks,
  question intake, answer export, domain-pack validation, fleet status, and MCP
  serving.
- Reusable workspace template, domain packs, orchestrator guidance, and a
  synthetic worked example.
- Python 3.10+ support on Windows, macOS, and Ubuntu under the MIT License.
