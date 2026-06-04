"""Entry point for python -m debridnzbd."""

import uvicorn


def main():
    uvicorn.run(
        "debridnzbd.app:create_app",
        factory=True,
        host="127.0.0.1",
        port=8080,
    )


if __name__ == "__main__":
    main()