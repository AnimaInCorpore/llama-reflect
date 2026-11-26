# Project skills

A project skill is a short Markdown file under `.refl/skills/` that explains how to reuse a tool or workflow that worked in the current project. Skills carry useful findings across sessions, even when a new conversation starts from scratch.

## Discovery and reuse

Reflect adds paths for `.refl/skills/*.md` to the model's context. The model can open a relevant note with `read_file` and use it to choose its next action. Use filenames that describe the task, such as `inspect-dos-com.md` or `run-integration-tests.md`. This makes it possible to find the right note from its path alone. The files are plain Markdown and can be reviewed, edited, or committed alongside the project's code.

Skills reuse the existing tools. The main branch can verify an installation with `bash`, run a command, and save or update a note with `write_file` or `edit_file`. A verifier can read the notes and check their claims against workspace evidence. The Bookworm reviewer looks for a relevant skill when progress stalls and can pass a proven command or procedure back to the main branch. The project skill index reflects files added during the current run, so they are available to the next model request as well as future sessions.

This repository includes two skills: a verification smoke test against a local server, and the paired reflection benchmark. [Test Reflect against llama-server](../.refl/skills/test-with-llama-server.md), [Benchmark reflection against llama-server](../.refl/skills/benchmark-reflection.md)

## From investigation to skill

1. Identify the mechanical step. Examples include decoding instructions, inspecting an object file, reproducing a build step, or extracting a test report.
2. Check for a project skill that covers it. Then look at project documentation, installed commands, or upstream documentation for a suitable tool. `command -v TOOL` is a Bash check for a tool on the current machine.
3. Run the proposed command on the input or on a representative sample. Inspect the output to confirm it answers the question. Record the executable and arguments that worked, plus any prerequisite that affected the result.
4. Write or update the skill once the workflow is useful to repeat. Include when it applies, the command, the expected evidence, and relevant limits. A later session can repeat the tool or recheck its assumptions if the environment changed.

Each note can follow the structure below.

```markdown
# Inspect a DOS COM program

Use this workflow for flat 16-bit DOS `.COM` files in this project.

## Command

Run `ndisasm -b 16 -o100h samples/hello.com` from the project root.

## Check

The first listed offset is 0x100. Compare the instructions around the entry
point with the expected startup sequence for the sample.

## Limits

Use a format-aware workflow for DOS EXE files. Treat embedded data in the
listing as data when inspecting the instructions.
```

The template uses a sample path. In a real project, use the command and input that you verified and note the observed result.

## Example: choosing a tool for x86 binaries

The following example shows how a skill can capture a tool choice for one kind of input. First identify the file format and execution mode. A flat 16-bit DOS `.COM` file starts at offset `0x100`, so `ndisasm -b 16 -o100h` can generate a static instruction listing with the matching origin. NDISASM treats its input as raw bytes. For a container format such as a DOS `.EXE`, use a workflow that accounts for headers and the executable's entry point. [NASM NDISASM manual](https://www.nasm.us/doc/nasmab.html)

When the question is how the program behaves at runtime, a tool like DOSBox can run DOS software and let you observe that behavior. A skill can record the successful emulator configuration, invocation, and observed result alongside a separate static disassembly note. [DOSBox Staging manual](https://www.dosbox-staging.org/0.83/manual/introduction/what-is-dosbox-staging/)

For example, a future task might ask why a related `.COM` binary exits early. Reflect can find the saved skill, generate the instruction listing with the same tool, and focus its investigation on the exit path. If the evidence points to an environment-dependent behavior, the emulator workflow gives it a reproducible way to check.
