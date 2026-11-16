# Lessons from two development retrospectives

Reflect needs a way to tell whether a task is progressing and what each check proves. Development retrospectives from porting work suggest a common method. Define a runnable acceptance path, use the right reference for each decision, and record evidence that a later session can reproduce.

## Give each reference a job

A sibling project can supply a build scaffold, a mature implementation can establish expected behavior, and earlier projects can supply realistic inputs and test methods. A deterministic reference makes automated checks practical. An original executable can settle behavior when two implementations disagree.

For Reflect, a task or saved skill can name the authority for the desired behavior, a working example for how to build or run it, and a workload for checking it. A reflection reviewer can look for the missing reference when the main branch is guessing. Claims carried across projects should keep their original scope and link to the source that established them.

## Start with one real route

Choose a user-visible route someone can actually try. A complete workload can reveal failures in timing or coordination that small component checks miss. Trace the original behavior for the same route when a reference is available.

Reflect can establish an acceptance check early by naming the input, expected user-visible result, target environment, and one command or procedure that reproduces it. Verify the actual interface or artifact the user receives. A coding-agent smoke test can check the CLI exit, the file it changed, and the observed branch in the saved session. Those observations support a stronger conclusion than a final assistant message saying the edit happened.

## Compare matched evidence and check that the run advanced

Verify that comparison runs start from matching states and advance through the intended workload. Identical output can come from a stalled execution. Output can also match a reference while missing a real-time deadline. Check scenario identity, evidence of progress, fidelity, and timing.

Phase 3 can look for repeated errors and for checks that keep returning the same result without reaching an expected milestone. Examples are a command that exits successfully while leaving the expected artifact unchanged, or a benchmark that emits repeated samples without advancing its checkpoint. Before choosing a remedy, a reviewer should identify the actual path, match the inputs and environment to the reference, and propose a small test that would show progress. Use the same workload before and after a change and keep behavior and timing results separately visible.

## State what was verified

Record the scope of the result and the version and environment it came from. For example, a disassembly can establish which instructions are present. A trace from a running program can establish which instructions executed for a particular input. A physical device test provides evidence about that device and configuration.

Reflect can report a finding as an observation tied to an input, source, build, environment, and outcome. A text lookup can support a claim about what a file contains. A build or runtime claim needs a corresponding run on the main branch. A report from the user can guide reproduction and can be recorded as a report until the relevant check runs. If completing the task requires a trade-off that changes what the user asked for, show the evidence and bring the specific choice back to the user.

## Make successful investigations reusable

Keep working launch and test procedures with the project. Share validated methods with other projects and record unresolved questions at handoffs. Later work then has an identifiable starting point.

A useful Reflect skill records why the technique applies, the exact command that worked, the input and environment used, the signal to look for, and the limits of the result. When checking a new case, use that skill to pick a tool and rerun the relevant check. When a case fails in a way that changes how the next session should work, save the reproduction and the open question with the project alongside the successful workflow. [Project skills](project-skills.md)
