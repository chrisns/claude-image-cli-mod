# Security

inline-images reads output that any program, file or web page can control. It decodes images, runs a helper process, opens files when you click, and in iTerm2 writes to your terminal. So security reports matter to this project.

## Report a problem

Do not open a public issue for a security problem. Use [a private vulnerability report](https://github.com/chrisns/claude-image-cli-mod/security/advisories/new) on GitHub.

Tell us:

- what an attacker controls (tool output, an image file, a file name),
- what happens,
- the steps to make it happen.


## What the mod does to stay safe

- It decodes only PNG, JPEG, GIF, WebP, BMP, TIFF and ICO. It checks the first bytes of the data before any decoder runs. PostScript, PDF and SVG never reach Ghostscript or ImageMagick.
- It refuses images with too many pixels before it decodes them.
- It keeps its cache in a folder that only your user can read, and it writes each file atomically.
- A click opens the cached copy of an image, with the extension of the format that was decoded. It never opens a path that the tool output names.
- It removes control and format characters from file names before it shows them.
- It scans untrusted text in linear time.
- The iTerm2 overlay writes only base64 image data and cursor moves to the terminal.
