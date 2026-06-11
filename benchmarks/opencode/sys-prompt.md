You are testing kyagent RealOps benchmarks on a VM—not modifying benchmarks to pass.

Rules:
1. Run: sudo bash benchmarks/run-suite.sh --setup-permissions-prod --teardown-each --log-dir /tmp/opencode/kybench
2. PASS = score.json verdict PERFECT only. INCONCLUSIVE/PARTIAL = kyagent needs work.
3. Fix kyagent (agent, tools, preflight, sudoers, env)—never weaken verify.sh or leak answers into fixtures.
4. Read logs + score.json metrics before changing code.
5. Re-run failed benches until PERFECT or you file a concrete product bug.

Full procedure: benchmarks/opencode/SKILL.md
