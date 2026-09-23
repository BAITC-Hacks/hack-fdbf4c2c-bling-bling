import logging


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("app").setLevel(level)
    logging.getLogger("faster_whisper").setLevel(logging.WARNING)
    # Request bodies, meeting titles and model prompts must not be logged.
