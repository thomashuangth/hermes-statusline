import json, subprocess
argv = json.loads(subprocess.check_output(["hermes", "--print-runtime-command"], text=True))
index = argv.index("-c") + 1
entry = "runpy.run_module('hermes_cli.main', run_name='__main__', alter_sys=True)"
if not argv[index].endswith(entry):
    raise SystemExit("Unsupported launcher shape: stop and inspect this installation.")
lines = [
    "from pathlib import Path",
    "import json",
    "from hermes_constants import get_hermes_home",
    "from gateway.control_socket import reload_gateway_plugins",
    "home = Path(get_hermes_home())",
    "result = reload_gateway_plugins(home, profile_home=home)",
    "print(json.dumps(result, indent=2))",
    "if not isinstance(result, dict) or result.get('reloaded') is not True:",
    "    raise SystemExit('No successful live reload acknowledgement.')",
]
argv[index] = argv[index][:-len(entry)] + "exec(" + repr("\n".join(lines)) + ")"
subprocess.run(argv, check=True)