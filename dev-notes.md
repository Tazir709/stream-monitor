# Development Notes

These are personal development notes and potential future improvements.
They are not an assigned task list or project roadmap.

Some items are ideas I'm considering, while others are known issues
that I may address in a future update.

## Current Notes

### Chromium cookies on Windows

Add a `cookies.txt` fallback for Chromium browsers on Windows.

yt-dlp's browser-cookie extraction can fail due to Chromium's Windows
cookie encryption/locking. Firefox-based browsers currently work normally.

### Manual download stops

Ignore manually stopped downloads when applying short-download auto-start
protection.

Test case:

`streamer1 - 03:55 downloading → manual stop → should NOT auto-disable`

### User-Agent error guidance

Improve error guidance when stream access fails with no User-Agent.

Consider the selected browser/cookie source and platform first, so Windows
Chromium cookie errors aren't incorrectly attributed to the User-Agent.

### Cookie testing

Consider adding a **Test Cookies** button to the Settings dialog.

The button could run a quick yt-dlp check to verify that the selected browser
or `cookies.txt` source can successfully authenticate.

---

These notes are open to other ideas and improvements.
