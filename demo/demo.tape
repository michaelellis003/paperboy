# Records assets/paperboy-demo.gif. Run from this directory: vhs demo.tape
#
# The launch and the first turn run hidden so the session header (account
# name, plan, promo banners) scrolls off before anything is captured. The
# GIF opens on the search results, then two prompts are typed on camera.
#
# Prerequisites: the repo .env is configured; "Attention Is All You Need"
# is in Zotero tagged sent-to-ereader; no "State Space Models" collection
# exists and the take's three papers are not in the library (delete them
# in Zotero between takes or the dedup will change the conversation).
Output paperboy-demo.gif
Set Shell zsh
Set FontSize 14
Set Width 1200
Set Height 500
Set TypingSpeed 30ms
Set Theme "Catppuccin Mocha"
Hide
Type "cd /Users/michaelellis/Projects/paperboy && clear"
Enter
Sleep 1s
Type "claude --strict-mcp-config --mcp-config demo/mcp.json --allowedTools 'mcp__paperboy,WebSearch,WebFetch'"
Enter
Sleep 15s
Type "Use paperboy to find 3 recent papers on sequential Monte Carlo methods for state space models. List each with authors, year, and a 1-2 sentence summary."
Sleep 1s
Enter
Sleep 150s
Show
Sleep 6s
Type "Queue all three in Zotero and file them under a new State Space Models collection"
Sleep 1.2s
Enter
Sleep 12s
Hide
Sleep 150s
Show
Sleep 7s
Type "Now send the first two to my Kindle. Also send Attention Is All You Need."
Sleep 1.2s
Enter
Sleep 12s
Hide
Sleep 220s
Show
Sleep 10s
