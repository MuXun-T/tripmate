import uvicorn


def main() -> None:
    uvicorn.run("app.api:app", host="0.0.0.0", port=8000, factory=False)


if __name__ == "__main__":
    main()
