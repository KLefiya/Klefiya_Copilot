from __future__ import annotations

import unittest

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from backend import main


def _client(origins: list[str]) -> TestClient:
    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["GET", "PUT", "POST"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return TestClient(app)


class BackendCorsTests(unittest.TestCase):
    def test_default_origins_are_preserved(self) -> None:
        self.assertEqual(
            main.DEFAULT_ALLOWED_ORIGINS,
            ["http://localhost:5173", "http://127.0.0.1:5173"],
        )
        self.assertEqual(main._allowed_cors_origins(None), main.DEFAULT_ALLOWED_ORIGINS)

    def test_backend_accepts_launcher_custom_origin(self) -> None:
        origins = main._allowed_cors_origins("http://127.0.0.1:51987")
        client = _client(origins)
        response = client.get("/api/health", headers={"Origin": "http://127.0.0.1:51987"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("access-control-allow-origin"), "http://127.0.0.1:51987")

    def test_other_origin_is_not_allowed(self) -> None:
        client = _client(main._allowed_cors_origins("http://127.0.0.1:51987"))
        response = client.get("/api/health", headers={"Origin": "http://127.0.0.1:51988"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("access-control-allow-origin", response.headers)

    def test_wildcard_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Wildcard"):
            main._allowed_cors_origins("*")

    def test_credentials_query_fragment_and_path_are_rejected(self) -> None:
        invalid = [
            "http://user@127.0.0.1:5173",
            "http://user:pass@127.0.0.1:5173",
            "http://127.0.0.1:5173?x=1",
            "http://127.0.0.1:5173#frag",
            "http://127.0.0.1:5173/app",
        ]
        for origin in invalid:
            with self.subTest(origin=origin):
                with self.assertRaises(ValueError):
                    main._allowed_cors_origins(origin)

    def test_public_origin_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "loopback"):
            main._allowed_cors_origins("https://example.test")

    def test_duplicate_origin_is_deduplicated_and_root_path_normalized(self) -> None:
        origins = main._allowed_cors_origins("http://127.0.0.1:5173,http://127.0.0.1:51987/")
        self.assertEqual(origins.count("http://127.0.0.1:5173"), 1)
        self.assertIn("http://127.0.0.1:51987", origins)

    def test_malformed_env_fails_safely(self) -> None:
        for value in ["not-a-url", "", "http://"]:
            with self.subTest(value=value):
                if value == "":
                    self.assertEqual(main._parse_extra_cors_origins(value), [])
                else:
                    with self.assertRaises(ValueError):
                        main._allowed_cors_origins(value)


if __name__ == "__main__":
    unittest.main()
