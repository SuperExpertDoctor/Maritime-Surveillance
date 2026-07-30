import os


class EnvLoader:
    """Load key=value pairs from a .env file into os.environ."""

    @staticmethod
    def load_dotenv(dotenv_path: str = "configs/.env") -> None:
        """Load .env into os.environ.

        Lines that are blank or start with '#' are ignored.
        Does NOT overwrite existing environment variables.
        """
        if not os.path.isfile(dotenv_path):
            return

        with open(dotenv_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
