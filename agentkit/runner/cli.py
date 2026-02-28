import shutil
import sys

def doctor() -> int:
    required = ["git", "python3"]
    optional = ["rg"]  # ripgrep helps later but not required

    missing = [c for c in required if shutil.which(c) is None]
    if missing:
        print("Missing required commands:", ", ".join(missing))
        return 1

    print("✅ Required commands OK:", ", ".join(required))

    missing_opt = [c for c in optional if shutil.which(c) is None]
    if missing_opt:
        print("ℹ️ Optional commands not found:", ", ".join(missing_opt))
    else:
        print("✅ Optional commands OK:", ", ".join(optional))

    print(f"Python: {sys.version.split()[0]}")
    return 0

def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "doctor":
        raise SystemExit(doctor())
    print("agentkit is installed. Try: agentkit doctor")