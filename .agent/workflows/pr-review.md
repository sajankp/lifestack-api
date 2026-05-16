---
description: Review and process open GitHub PRs
---

# GitHub PR Review Workflow

Use this workflow to systematically review and process open PRs.

> [!CAUTION]
> **Global Rules Apply Throughout This Workflow**
> - Never commit without explicit user approval ("AGREE", "YES", "PROCEED")
> - Never merge or close a PR without explicit user approval
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

## Step 2: Wait for AI Review

> [!IMPORTANT]
> Ensure `gemini-code-assist` (or other required AI reviewers) has reviewed the PR before proceeding.

Check for formal reviews, inline code comments, or general conversation comments from the AI reviewer.
- If no feedback or review exists: **WAIT**.
- If there is feedback or a review: Proceed to Step 2.1.

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

Retrieve specific review comments programmatically to understand the context or reply to a thread. Use the GitHub MCP tool (e.g., `mcp_github-mcp-server_pull_request_read` with method `get_review_comments`) if available, or fall back to the `gh` CLI/API.

### Step 2.4: Address Feedback and Commit

> [!CAUTION]
> **STOP AND ASK BEFORE COMMITTING**
> This step involves code changes. Follow the same commit protocol as `/development-workflow`.

**When fixing issues from review:**

1. **Make the code changes**

2. **Run local tests** (Gatekeeper Rule - MANDATORY):
   ```bash
   pytest --cov=app app/tests/ -q
   ```
   All tests must pass before proceeding.

3. **Stage changes**:
   ```bash
   git add <files>
   ```

4. **STOP - Request user approval**:
   ```
   Message: "Ready to commit PR feedback fixes with message:
            'fix: address review feedback on [feature]'
            Local tests passing. Proceed?"
   ```

5. **Only commit after explicit approval** ("yes", "proceed", "commit")

6. **Verify commit succeeded**:
   ```bash
   git log --oneline -1  # Confirm commit appears
   git status            # Check for uncommitted changes (pre-commit can fail)
   ```
   If `git status` shows changes → pre-commit failed → fix and retry.

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



### Step 2.6: Request Re-Review (After Addressing Feedback)

After fixing issues identified by the AI reviewer, explicitly request a fresh review to ensure your changes are validated.

You can do this by:
- Re-requesting a review from the reviewer directly (e.g. via UI).
- Adding a comment such as `/gemini review` if the bot supports slash commands.

> [!TIP]
> Option A (UI) is cleanest. The `/gemini review` command also works and explicitly requests a new review.

**Other useful commands:**
- `/gemini summary` - Get updated PR summary
- `@gemini-code-assist <question>` - Ask specific questions

Wait for the new review before proceeding to merge.

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
