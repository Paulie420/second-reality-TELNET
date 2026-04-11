# Credits

This project exists because of the work of others. Nothing here is original except the
glue code and documentation in this repository.

## Future Crew — *Second Reality* (1993)

[*Second Reality*](https://www.pouet.net/prod.php?which=63) was released by **Future Crew**
at Assembly 1993. It was and remains one of the most celebrated demoscene productions
ever made. This project streams a pre-rendered ANSI rendition of the demo's video
capture over telnet — it does **not** include, redistribute, or modify the original demo
or its source assets.

- Canonical archive: [scene.org](https://files.scene.org/)
- Demoscene database entry: [pouet.net — Second Reality](https://www.pouet.net/prod.php?which=63)
- Future Crew released the original source code of *Second Reality* in 2013. It is
  available via the above archives.

If you haven't seen the demo in its original form, please watch it there first. The
terminal rendition is a love letter, not a replacement.

## Jeff Quast (@jquast) — `blessed`, `telnetlib3`, `network23`

This project follows the architecture of **`network23`**, the backend behind
[`telnet 1984.ws`](https://1984.ws), written by Jeff Quast in a one- or two-day hackathon
and generously released to the public domain:

> "you're welcome to run it, copy/distribute, or whatever, public domain!"
>
> — Jeff Quast, personal correspondence, 2025-04-16

The reference source (~721 MB, mostly pre-rendered frames for the Max Headroom clip) is
available at [jeffquast.com/network23.tar.gz](https://jeffquast.com/network23.tar.gz).

`second-reality-TELNET` uses two of jquast's Python libraries directly:

- [`blessed`](https://github.com/jquast/blessed) — terminal capability wrapper for clean
  cursor positioning and ANSI sequence generation.
- [`telnetlib3`](https://github.com/jquast/telnetlib3) — asyncio telnet server library
  with proper IAC / NAWS / option negotiation.

The architectural pattern (NAWS-based width bucket picker, per-connection coroutine,
centered frame placement with margins, frame skipping on slow clients) is directly
copied — with gratitude — from `network23/shell.py`. Any improvements made here
(in-memory frame caching, higher frame rate, parameterized source) are incremental
refinements on jquast's template.

## `chafa` — Hans Petter Jansson

[`chafa`](https://hpjansson.org/chafa/) is the image-to-ANSI renderer that does the
actual hard work of turning pixels into best-fit truecolor block characters. Every frame
you see over telnet was produced by piping a PNG through `chafa`.

- Project: https://hpjansson.org/chafa/
- Source: https://github.com/hpjansson/chafa

## `ffmpeg`

[`ffmpeg`](https://ffmpeg.org) is used to extract frames from the source video at the
target frame rate and pass them to `chafa`. As always, thank you to the ffmpeg
maintainers.

## Everyone running a BBS or telnet art server in 2025+

You keep the vibe alive. Thank you.
