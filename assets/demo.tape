# README demo. Regenerate with:  vhs assets/demo.tape   (https://github.com/charmbracelet/vhs)
# Needs `hyperdata` on PATH. The hub warms up off camera (positions take ~30s,
# smart money tiers a few minutes), so the recording shows the typed command
# and then the live combined view.

Output assets/demo.gif

Set Shell "bash"
Set FontSize 14
Set Width 1500
Set Height 960
Set Padding 18
Set Framerate 12
Set Theme "Catppuccin Mocha"
Set TypingSpeed 55ms

Hide
Type "clear" Enter
Show

Type "hyperdata all --no-boot"
Sleep 600ms
Enter
Hide
Sleep 240s
Show
Sleep 9s
