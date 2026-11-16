# Test Reflect against llama-server

Use this workflow to check tool calling and verification after changing Reflect. Use a tool-capable GGUF and run the test against a disposable workspace.

Start a local server with two slots. The CPU configuration below was used with Qwen2.5-1.5B-Instruct Q4_K_M on the local llama.cpp build.

```text
llama-server -m model.gguf --host 127.0.0.1 --port 8081 --jinja -np 2 -c 8192 --reasoning off --no-ui --device none
```

If the binary is outside `PATH` on Windows, launch the built `llama-server.exe` directly.

In the test workspace, create `status.txt` containing `old_ready`. Ask Reflect to copy the tag as its first reply and report the verdict when it arrives.

```text
refl --url http://127.0.0.1:8081 --workspace ./test-workspace "Copy <verify>status.txt contains old_ready</verify> verbatim as your first reply. Wait for the verification results and report the verdict."
```

Expect a `verify read_file status.txt` or `verify grep_search …` event followed by `[VERIFIED]`. Inspect the session JSON to confirm there is a verification branch with a tool result and a main branch that received the verdict. Save the session ID if you need to test `--resume` after changing the fixture.
