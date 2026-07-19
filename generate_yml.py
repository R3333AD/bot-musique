import os
import re

ENV_PATH = ".env"
EXAMPLE_PATH = "application.yml.example"
OUTPUT_PATH = "application.yml"


def main():
    env = {}
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    key, val = line.split("=", 1)
                    env[key.strip()] = val.strip()

    if not os.path.exists(EXAMPLE_PATH):
        print("[WARN] application.yml.example not found, skipping generation.")
        return

    with open(EXAMPLE_PATH, encoding="utf-8") as f:
        content = f.read()

    password = env.get("LAVALINK_PASSWORD", "youshallnotpass")
    content = content.replace("${LAVALINK_PASSWORD}", password)

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"[INFO] Generated {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
