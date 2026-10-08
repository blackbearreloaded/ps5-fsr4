# Pull-request builds

Every pull request is built in full by the [Build workflow](../.github/workflows/build.yml):
the driver, the FSR4 runtime, the SDK, the host tests and the same packaging a release
gets. The result is an installable copy of the [showcase app](../examples/fsr4_showcase/README.md)
(PPSA99011), kept for 14 days, that a reviewer can put on a console before merging.

## What a pull request produces

| | Pull request | Tag, manual run |
| --- | --- | --- |
| Artifact name | `ps5-fsr4-PR<number>-<commit>` | `ps5-fsr4-showcase-<commit>` |
| `<commit>` | First seven characters of the pull request's own head commit | The full commit that was built |
| `build-label.txt` in the app folder | `PR <number>, <commit>` | None |
| Version | Unchanged (`0.0.0-dev`, `contentVersion` 01.000.000) | Unchanged |

Two details are deliberate:

- **The commit is the pull request's head**, not `github.sha`. For a pull request,
  `github.sha` is a temporary merge commit that appears nowhere on the pull request's page,
  so an artifact named after it cannot be matched to what is being reviewed.
- **The version is not touched.** The ZIP inside the artifact is still called
  `ps5-fsr4-showcase-0.0.0-dev-PPSA99011.zip`, and `param.json` is what a build of `main`
  has. The label is a separate file.

The pull-request name starts with the repository's name, so a fork's own pull requests are
named after the fork.

## Getting the build

1. Open the pull request, then **Checks** and the **Build** run (or the run's page under
   **Actions**).
2. Download the artifact named `ps5-fsr4-PR<number>-<commit>` from the run's **Artifacts**
   list. GitHub requires a signed-in account for this.
3. Unpack it: it holds `ps5-fsr4-showcase-0.0.0-dev-PPSA99011.zip`. That ZIP holds the
   `PPSA99011` folder, a README, `notices/` and `SHA256SUMS`, which lists every file,
   `PPSA99011/build-label.txt` included. Every entry is stored with permissions 0777, as
   the console wants an app's files. Upload the `PPSA99011` folder to `/data/homebrew/`.

A first-time contributor's pull request does not build until a maintainer approves the
workflow run. That is GitHub's default for public repositories and is worth keeping: the
build runs the pull request's code.

## The label inside the app folder

`tools/build_fsr4_showcase.py` writes the environment variable `BUILD_LABEL` as one line to
`build-label.txt` at the root of the app folder, next to `eboot.bin`
(`/app0/build-label.txt` on the console). The workflow sets it for pull requests only. A
build without it writes no file, and removes one left in the build folder by an earlier
build, so a release never carries one.

`BUILD_LABEL` must be 1 to 40 characters from letters, digits, spaces and `, . _ # -`. The
build refuses anything else before compiling, so the text is safe to show as it is.

The showcase does not read the file yet: its settings menu shows the version
(`PS5 FSR4 Showcase 0.0.0-dev` for any build that is not a release) and nothing else. To
tell which build is installed, read `build-label.txt` in the installed folder.

The same works on your PC, for a build you want to tell apart on the console:

```bash
BUILD_LABEL="pacing test 2" make showcase
```

## What not to change

Pull-request runs have a read-only token and no secrets, including for forks. Do not move
this build to `pull_request_target` to post links or comments: that event runs with write
access and secrets, and building a contributor's code under it hands both to that code.
The `release` job reads the artifact called `release`, which only manual runs and tags
upload, so a pull request's name never reaches it.
