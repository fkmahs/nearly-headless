# Answer Deck

A small app for the Claude desktop app. It shows your Claude Code chats that are waiting on you as a stack of cards, the one to answer first on top.

- Each card shows the chat's latest reply, what it asks you, and why it is in that place.
- **Open chat** opens that chat in the Claude desktop app. When you reply there, the card checks itself off and the next one comes forward.
- **Previous** and **Next** (or the arrow keys, or a swipe) move through the stack. A ring counts what you have answered.

It runs on Windows, macOS and Linux, as an app installed from the Claude desktop app. To install it, ask your AI agent to follow [INSTALL.md](INSTALL.md).

## What it reads

Only files on your own computer, and only to read them:

- your Claude Code chats changed in the last 7 days (`~/.claude/projects`, top-level chats only);
- the Claude desktop app's list of its chats, for each chat's title, whether it is archived, and the link that opens it.

It never changes, sends or deletes anything in your chats. To read the end of a very large chat file it runs your computer's own tools (PowerShell on Windows, `tail` elsewhere); to open a chat it hands a `claude://` link to the system.

## When it uses Claude

Only to put new waiting replies in order. When a chat has a reply you haven't seen ranked yet, the app sends that reply (up to its first 4,000 characters), the chat's title and how long it has waited to Claude, on your own account, and asks for a score and a one-line reason. Claude asks your permission the first time. If you say no, or Claude isn't available, the app orders the cards by a simple rule instead and says so.

Checking whether you have replied makes no Claude call: it looks at the front chat's file every few seconds.

## Privacy

- Your chats stay on your computer. The only text that leaves it is the waiting replies sent to Claude for ranking, as above.
- The page loads fonts from Google Fonts and two small libraries (for showing replies safely) from cdnjs.
- The order and the answered marks are kept in the app's own storage on your computer.
- The artifact is published to your own account and is private unless you share it. Sharing it shares the app, never your chats: each person who installs it sees only their own.
