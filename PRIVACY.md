# Privacy Policy

_Last updated: 2026-07-11_

Moniker is a Discord bot that renames your server from community-submitted quotes and runs optional bracket championships. This policy explains what data it stores, why, and how to remove it. Plain language, no surprises.

## What Moniker stores

- **Server settings**: the channels, schedule, timezone, and options you configure. Needed to run the bot.
- **Submitted content from the channels you designate**: for a quote or icon channel you point Moniker at, it reads messages to pick daily content, and it stores the selected quote text or image link, plus the submitter's Discord user ID and display name at the time it was picked.
- **A log of past picks**: user ID, name, the item picked, and when. Used for fair rotation (so the same person or quote does not repeat) and for `/mystats`.
- **Rename and bracket records**: the quotes posted on rename cards, who submitted them, message IDs, and bracket results. Used to seed and run brackets.

## What Moniker does NOT do

- It does not read your DMs, or any channel you have not configured it to use.
- It does not collect payment information (the bot is free).
- It does not use analytics or tracking.
- It never sells or shares your data with third parties.

## The Message Content permission

Moniker uses Discord's Message Content privileged intent for one purpose: to read the quote, icon, and custom-feature channels you configure, so it can pick content. It is not used to read anything else.

## Where the data lives

In a database on the bot's host (currently Railway), stored per server and separated by server. One server's data is never mixed with or exposed to another.

## Retention and deletion

You are always in control of your server's data:

- **Delete it any time**: a server admin (Manage Server) can run `/admin reset` to permanently wipe everything Moniker stores for that server.
- **Remove the bot**: if Moniker is removed from your server, its data for that server is automatically deleted after 30 days. Re-adding the bot within those 30 days keeps your setup; after that, it is gone.

## Changes

If this policy changes, the updated version will be posted here with a new date.

## Contact

Questions or a deletion request? [Open an issue](https://github.com/LoopRook/Multivitamin/issues).
