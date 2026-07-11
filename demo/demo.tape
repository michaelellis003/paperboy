# Records assets/paperboy-demo.gif. Run from this directory: vhs demo.tape
#
# The whole session is on camera, from launching Claude Code onward.
# Recording pauses (Hide) only while the model is thinking, so the GIF
# stays short; the full conversation text is visible after every turn.
#
# Prerequisites: the repo .env is configured; "Attention Is All You Need"
# is in Zotero tagged sent-to-ereader; no "State Space Models" collection
# exists and the take's three papers are not in the library (delete them
# in Zotero between takes or the dedup will change the conversation).
Output paperboy-demo.gif
Set Shell zsh
Set FontSize 14
Set Width 1200
Set Height 720
Set TypingSpeed 30ms
Set Theme "Catppuccin Mocha"
Hide
Type "cd /Users/michaelellis/Projects/paperboy && clear"
Enter
Sleep 1s
Show
Sleep 1s
Type "claude --strict-mcp-config --mcp-config demo/mcp.json --allowedTools 'mcp__paperboy,WebSearch,WebFetch'"
Sleep 500ms
Enter
Hide
Sleep 15s
Show
Sleep 2.5s
Type "Use paperboy to find 3 recent papers on sequential Monte Carlo methods for state space models. List each with authors, year, and a 1-2 sentence summary."
Sleep 1s
Enter
Sleep 12s
Hide
Sleep 150s
Show
Sleep 9s
Type "Queue all three in Zotero and file them under a new State Space Models collection"
Sleep 1.2s
Enter
Sleep 12s
Hide
Sleep 150s
Show
Sleep 8s
Type "Now send the first two to my Kindle. Also send Attention Is All You Need."
Sleep 1.2s
Enter
Sleep 12s
Hide
Sleep 220s
Show
Sleep 10s
