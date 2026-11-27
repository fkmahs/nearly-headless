# Installing Answer Deck (for an AI agent)

You are an AI agent helping a person install Answer Deck ([README](README.md)) on their own computer, with their own Claude account. You publish the app as their private artifact; **they** install it and grant its permissions. These steps are written for Claude Code in the Claude desktop app; another agent can follow them if it has the tools named in "What you need".

## Rules

- **Never approve a prompt for the person.** Do not click Install, Allow, or any permission or consent dialog, and do not drive the screen or browser to do it. The person clicks each one.
- **Publish the file exactly as it is.** Don't edit it, minify it, or add anything. Keep the `claude-artifacts-manifest` tag near the top: it tells the desktop app which permission the app uses (`full-access`).
- **Keep it private.** Publish it to the person's own account and don't share or pin it unless they ask.

## What you need

1. **The Claude desktop app**, signed in, on Windows, macOS or Linux. Answer Deck runs only inside it, as an installed app.
2. **Claude Code chats on this computer**: the folder `~/.claude/projects` exists. Without it the app installs but shows "No chats in the last 7 days".
3. **A tool that publishes a claude.ai artifact with capabilities.** In Claude Code that is the `Artifact` tool. If your Artifact tool asks you to load a design or capabilities skill before publishing, load it, but still publish this file unchanged. If you have no such tool, stop and tell the person: "I can't publish artifacts from here. Open this in Claude Code in the Claude desktop app and ask again."

## Steps

### 1. Get the file

Use `apps/answer-deck/answer-deck.html` from a clone of this repo, at the version the person wants (the latest `main` by default). With no clone, download it into a new empty folder (your scratch folder, if you have one):

```
https://raw.githubusercontent.com/fkmahs/nearly-headless/main/apps/answer-deck/answer-deck.html
```

Read the whole file before publishing it. It should start with `<title>Answer Deck</title>` and contain the line:

```html
<script type="application/json" id="claude-artifacts-manifest">{"permissions": ["full-access"]}</script>
```

If either is missing, stop and tell the person the file looks wrong.

### 2. Publish it as the person's private artifact

One call to the `Artifact` tool:

| Field | Value |
|---|---|
| `action` | `publish` (or leave it out) |
| `file_path` | the local path of `answer-deck.html` |
| `capabilities` | `{"desktop": {}, "sample": {}}` |
| `icon` | `cards` |
| `description` | `Waiting Claude Code chats as a stack of cards, the one to answer first on top.` |

Don't pass `url` (that would overwrite another artifact). `desktop` lets the person install the page as an app that reads their chat files; `sample` lets the app ask Claude to order the cards. The result gives the artifact's link. Write the link down in your reply: the person needs it to update later.

If the publish is refused, tell the person the reason it gives, word for word, and stop.

### 3. Hand over to the person

Tell the person, in these words or close to them:

> Answer Deck is published to your account, private: <link>
> 1. Open that link in the Claude desktop app. You'll see an example deck and an **Install as app** button. Click it and confirm.
> 2. Open **Answer Deck** from your apps when it's installed.
> 3. Click **Allow access** so it can read your chats (it only reads them).
> 4. The first time it orders your cards, Claude asks whether the app may use it. Allow it for Claude's order, or decline for a simple rule-based order.

Then wait. Don't click anything for them.

### 4. Check it works

You can check the publish yourself: `Artifact` with `action: "read"` and the link returns the page; it should match the file you published.

The rest the person sees, so ask them what they see and compare:

| Where | What they should see |
|---|---|
| The link, in the Claude desktop app, before installing | an example deck (cards marked "Example") and a box "Install Answer Deck as an app" with an **Install as app** button |
| The link in a web browser | the example deck and "Open this in the Claude desktop app" (expected: it installs only from the desktop app) |
| The installed app, before access | "Let Answer Deck read your chats" with **Allow access** |
| The installed app, after access | their own chats as cards; the header says something like "1 of 4 · 3 waiting · updated 5s ago", and the tag beside **Refresh** says "Ordered by Claude" (or "Ordered by rule" and why) |
| **Open chat** on the front card | that chat opens in the Claude desktop app |

If something differs:

- **"Access is turned off"**: the person declined access. They click **Open permissions** in the app and turn full access on.
- **"Access isn't available right now"**: the desktop app is still signing in, or another organization is selected. They switch back and press **Try again**.
- **"No chats in the last 7 days"**: no Claude Code chat on this computer changed in the last week. Expected on a new computer.
- **"Ordered by rule · Claude was not allowed"**: the person declined Claude. The deck still works; reopening the app asks again.
- **No Install as app button in the desktop app**: the person's account or organization may not allow desktop apps. Tell them; there's nothing for you to change.

### 5. Updating it later

When the person asks to update Answer Deck:

1. Get the newer `answer-deck.html` as in step 1, and read it.
2. Read the existing artifact first: `Artifact` with `action: "read"` and its link. If you don't have the link, `action: "list"` shows the person's artifacts; look for the one titled "Answer Deck", and ask the person if there's more than one.
3. Publish to the same artifact: `Artifact` with `file_path` and that link as `url`. Leave `capabilities` out to keep the ones it has, and leave `icon` out to keep its icon.
4. Tell the person to close and reopen Answer Deck. The app may ask again for access or for Claude, because it is a newer version; they allow it themselves.

Never publish without `url` when updating: that makes a second, separate artifact.

### Removing it

The person uninstalls the app from the Claude desktop app themselves. Delete the artifact only if they ask you to, after confirming that it can't be undone.
