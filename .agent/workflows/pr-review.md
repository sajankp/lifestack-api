---
description: Review and process open GitHub PRs
---

# GitHub PR Review Workflow

Use this workflow to systematically review and process open PRs.

> [!CAUTION]
> **Global Rules Apply Throughout This Workflow**
> - Avoid stopping for approvals before committing; verify tests locally and commit directly once verified.
> - At the start of a task, always clarify and ask the user what changes/remediations are required.
> - Never merge or close a PR without explicit user approval.
> - Verify every commit with `git log` + `git status`
> - Run local tests before pushing (Gatekeeper Rule)
>
> See: Global User Protocols in your user rules

## 🧭 When to Use This Workflow

```
Do I have open PRs to process?
├── YES → Use this workflow
└── NO → Is there a new PR from Dependabot/other?
         └── Run `gh pr list --state open` to check
```

---

## Step 0: Pre-Review Verification (For Feature PRs)

> [!IMPORTANT]
> Before processing a PR that came from `/development-workflow`, verify compliance.

For PRs implementing features (not Dependabot/minor fixes), confirm:

```
┌────────────────────────────────────────────────────────────┐
│  PRE-REVIEW CHECKLIST                                      │
├────────────────────────────────────────────────────────────┤
│  □ Spec exists in docs/specs/ and status is "Implemented"  │
│  □ Tests exist in the PR (TDD was followed)                │
│  □ Full test suite passed locally (Gatekeeper Rule)        │
│  □ All development workflow commits were user-approved     │
│  □ Phase 5 verification was completed                      │
└────────────────────────────────────────────────────────────┘
```

**If ANY item is missing** → Do not proceed. Return to `/development-workflow`.

---

## Step 1: List Open PRs

Retrieve the list of open pull requests in the repository to identify which ones need review or processing. Use the GitHub MCP tool (e.g., `mcp_github-mcp-server_list_pull_requests`) if available, otherwise fall back to the `gh` CLI (e.g., `gh pr list --state open`).

## Step 2: Check for AI Review

> [!NOTE]
> Gemini Code Assist on GitHub was sunset 2026-07-17 — all code review activity has ceased.
> Do not wait for a `gemini-code-assist` review; none will arrive. Check once
> (`gh pr view <n> --json reviews`) for a review from Gemini or any other AI reviewer actually
> configured for the repo.

Check for formal reviews, inline code comments, or general conversation comments from an AI reviewer.
- If one exists: proceed to Step 2.1 to triage it.
- If none exists — the expected default now — skip straight to Step 3 (Check Status). CI green
  + zero unresolved conversations is the real merge gate, not the presence of an AI review.

### Step 2.1: Fetch Feedback Details

If feedback exists, meticulously retrieve and analyze all feedback components:
1. Formal Reviews (e.g., Approve, Request Changes)
2. Inline Code Comments tied to specific files and lines
3. General Conversation Comments on the PR issue

### Step 2.2: Critically Evaluate Feedback

> [!IMPORTANT]
> **Do NOT blindly accept AI review comments.** Think critically about each suggestion.

For each review comment, ask yourself:

**1. Does this improve the code?**
- ✅ Better design/architecture
- ✅ Follows project conventions
- ✅ Fixes actual bugs
- ❌ Subjective style preference
- ❌ Contradicts documented patterns

**2. What's the trade-off?**
- Does it introduce breaking changes?
- Does it add complexity vs. value?
- Is it aligned with project goals?

**3. Is it correct for this codebase?**
- Check if it follows patterns in `AGENTS.md`
- Verify against existing code conventions
- Consider project-specific requirements

**4. ⚠️ SPEC INTEGRITY CHECK (Critical)**

> [!CAUTION]
> **Before accepting ANY suggestion, verify it's consistent with the approved Spec.**

- Does the suggested change align with `docs/specs/NNN-*.md`?
- If the suggestion contradicts the Spec:
  - **Do NOT accept it silently**
  - STOP and discuss with user
  - Options: Update Spec first, OR reject the suggestion with explanation

**Example evaluation:**
```
Comment: "Move DB write from utility to route handler"

Analysis:
✅ AGREE: Separation of concerns (utility does one thing)
✅ AGREE: Consistent with project's request.app.user pattern
⚠️  SPEC CHECK: Does Spec-010 define DB writes in utility? → No, spec is silent
✅ DECISION: Accept, it's architecturally superior AND spec-consistent
```

Document your reasoning and decision before making changes.

### Step 2.3: Get Review Comments (If Needed)

Retrieve and format all unresolved pull request review comments grouped by thread using the helper script:

```bash
# Display unresolved review threads formatted in the terminal
bash .agent/scripts/fetch-review-comments.sh

# Display all review threads, including resolved ones
bash .agent/scripts/fetch-review-comments.sh --all
```

Alternatively, use the GitHub MCP tool (e.g., `mcp_github-mcp-server_pull_request_read` with method `get_review_comments`) if available, or fall back to raw `gh` CLI/API.

### Step 2.4: Address Feedback and Commit

**When fixing issues from review:**

1. **Clarify changes**: Ask the user what needs to be changed before starting or describe the planned changes to align on scope.

2. **Make the code changes**

3. **Run local tests** (Gatekeeper Rule - MANDATORY):
   ```bash
   pytest --cov=app app/tests/ -q
   ```
   All tests must pass before proceeding.

4. **Stage changes**:
   ```bash
   git add <files>
   ```

5. **Commit directly**: Create the commit using a descriptive message e.g., `fix: address review feedback on [feature]`. (No commit approval is required).

6. **Verify commit succeeded**:
   ```bash
   git log --oneline -1  # Confirm commit appears
   git status            # Check for uncommitted changes (pre-commit can fail)
   ```
   If `git status` shows changes → pre-commit failed → stage and retry the commit.

7. **Push changes**:
   ```bash
   git push
   ```

### Step 2.5: Reply to Review Comments

After addressing feedback, document what was done by replying to each comment thread and explicitly resolving it.

> [!TIP]
> **Best Practice:**
> 1. Retrieve the thread IDs (e.g., using MCP `get_review_comments` or `gh api`).
> 2. **Add a reply comment** to each thread explaining what you fixed and referencing the commit SHA.
> 3. Resolve the thread using the available tool (MCP `mcp_github-mcp-server_pull_request_review_write` or GitHub UI/CLI).
> 4. The `isOutdated: true` flag indicates the code has changed since the comment.
> 5. After resolving all threads, the PR should be mergeable.



### Step 2.5.1: Optional CLI Helper to Resolve Threads

If GraphQL thread resolution is repetitive, use the helper script:

```bash
# Safer default: resolve only outdated unresolved threads
bash .agent/scripts/resolve-review-threads.sh --mode outdated

# Resolve all unresolved threads (use only when validated)
bash .agent/scripts/resolve-review-threads.sh --mode all

# Preview only
bash .agent/scripts/resolve-review-threads.sh --mode outdated --dry-run
```

Recommended flow:
1. Run `--dry-run` first
2. Resolve `--mode outdated`
3. Manually verify non-outdated unresolved comments before using `--mode all`


For targeted non-outdated threads that were verified manually, use:

```bash
# Single thread
bash .agent/scripts/resolve-specific-threads.sh --thread <thread_id> --reply "Addressed in commit <sha>: <summary>"

# Multiple threads from file (one ID per line)
bash .agent/scripts/resolve-specific-threads.sh --threads-file .tmp/thread-ids.txt --reply-file .tmp/reply.md

# Preview actions
bash .agent/scripts/resolve-specific-threads.sh --thread <thread_id> --dry-run
```


### Step 2.6: Request Re-Review (Applies Only If An AI Reviewer Actually Commented)

> [!NOTE]
> `/gemini review`, `/gemini summary`, and `@gemini-code-assist` are inert — Gemini Code
> Assist's GitHub integration was sunset 2026-07-17 and no longer acts on repo commands. This
> step only applies if some other configured AI reviewer left comments in Step 2. Otherwise
> skip straight to Step 3.

After fixing issues identified by a reviewer, explicitly request a fresh review from that
reviewer to ensure your changes are validated, then wait for it before proceeding to merge.

### Step 2.6.1: Post Re-Review Comments with Proper Newlines

When posting follow-up PR comments, use real multiline markdown. Do not send literal escaped newline text like `\n` in a single-line body.

Use:

```bash
gh pr comment <pr_number> --repo <owner>/<repo> --body-file - <<'EOF'
Follow-up fixes pushed for review findings and CI hardening:
- <fix 1>
- <fix 2>

Validation:
- <test command>

/gemini review
EOF
```

## Step 3: Check Status

Retrieve the current status of the pull request. Pay close attention to:
- The overall state (Open, Closed, Merged)
- Status checks and their conclusions (Success, Failure, Pending)
- The total number of comments and reviews

## Step 4: Check Logs BEFORE Rebasing

> [!CAUTION]
> **NEVER trigger a dependabot rebase without first checking failure logs.**

Examine the CI/CD pipeline failure logs for the pull request to diagnose the root cause of the failure before attempting a rebase.

## Step 5: Process PR

### If CI Passed and No Open Comments

For Dependabot PRs or other PRs with green CI and no open/blocking comments (e.g., prior comments instructing to skip a major version upgrade):

> [!CAUTION]
> **NEVER merge without explicit user approval.**
> Even if CI passes, you MUST request final sign-off.

1. **Request final merge approval**:
   ```
   Message: "PR #<NUMBER> is ready to merge:
            - CI: ✅ All checks passing
            - AI Review: ✅ All comments addressed/resolved
            - Tests: ✅ Passing locally

            Approve merge?"
   ```

2. **Only merge after explicit approval**:
   Execute the merge action for the pull request.

3. **Verify merge succeeded**:
   Confirm the pull request state is now "Merged".

### If CI Failed Due to Old Base Branch

Trigger a rebase by commenting on the PR (e.g., `@dependabot rebase`) or performing the rebase action.

### If CI Failed Due to Code Issues

> [!CAUTION]
> **NEVER close a PR without explicit user approval.**

> [!TIP]
> If CI fails due to missing functionality (not just bugs), consider whether a spec update is needed via `/development-workflow`.

1. Notify user & request approval
2. Create tracking issue
3. Close PR

**Track the blocked upgrade:**
- Create a new issue titled "Blocked: <package> <old> → <new>".
- Ensure the issue body describes the problem, the anticipated fix path, and links to the closed PR.
- Add appropriate labels like `dependencies` and `blocked`.

**Close the PR:**
- Post a comment on the PR indicating it is being closed and linking to the tracking issue.
- Execute the close action on the pull request.

## Step 6: Post-Merge Cleanup

After PR is merged:

- [ ] Update ROADMAP.md if applicable
- [ ] Close related issues
- [ ] Update spec status if needed
- [ ] Update ARCHITECTURE.md if system architecture changed

## Step 7: Update Documentation

If you encounter new patterns, add them to `AGENTS.md`.

---

*Workflow established: 2025-01-05*
*Tightened safety protocols: 2026-01-18*
