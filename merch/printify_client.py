"""
printify_client — create a product on Printify and publish it to Etsy as a draft.

Unlike Printful, Printify's API CAN create products on a connected Etsy store and
push them to Etsy. With the store's publishing set to "Manual approval", the
pushed product lands as an Etsy DRAFT (the review gate David wants).

Flow: upload_image(url) -> create_product(...) -> publish_product(id).
Printify requires a User-Agent header on every request.
"""
from __future__ import annotations
import time
import requests

BASE = "https://api.printify.com/v1"

# Transient-error retry policy: a momentary blip (network, 5xx, rate-limit) must
# never strand a design in _failed — it retries within the same run until it works.
_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 5
_BACKOFF = [2, 4, 8, 16]  # seconds between attempts


class PrintifyError(RuntimeError):
    pass


class PrintifyClient:
    def __init__(self, token: str, shop_id: str | int, timeout: int = 90):
        self.token = token
        self.shop_id = str(shop_id)
        self.timeout = timeout

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}",
                "User-Agent": "CrappyRV-Merch-Autoposter/1.0"}

    def _req(self, method: str, path: str, **kw) -> dict:
        last_err = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                r = requests.request(method, BASE + path, headers=self._headers(),
                                     timeout=self.timeout, **kw)
            except requests.RequestException as e:
                last_err = f"network error on {method} {path}: {e}"
            else:
                if r.status_code < 400:
                    return r.json() if r.text else {}
                if r.status_code not in _RETRY_STATUSES:
                    # A permanent error (bad request, auth, etc.) — don't retry.
                    raise PrintifyError(f"HTTP {r.status_code} on {method} {path}: {r.text[:400]}")
                last_err = f"HTTP {r.status_code} on {method} {path}: {r.text[:200]}"
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(_BACKOFF[attempt])
        raise PrintifyError(f"gave up after {_MAX_ATTEMPTS} attempts — {last_err}")

    # ---- steps ----
    def upload_image(self, file_name: str, url: str) -> str:
        """Printify fetches the image from `url`; returns the uploaded image id."""
        data = self._req("POST", "/uploads/images.json",
                         json={"file_name": file_name, "url": url})
        return data["id"]

    def create_product(self, product: dict) -> dict:
        return self._req("POST", f"/shops/{self.shop_id}/products.json", json=product)

    def publish_product(self, product_id: str | int) -> dict:
        # Publishing to a connected Etsy store; "Manual approval" mode -> Etsy draft.
        body = {"title": True, "description": True, "images": True, "variants": True,
                "tags": True, "keyFeatures": True, "shipping_template": True}
        return self._req("POST", f"/shops/{self.shop_id}/products/{product_id}/publish.json",
                         json=body)

    def delete_product(self, product_id: str | int) -> None:
        self._req("DELETE", f"/shops/{self.shop_id}/products/{product_id}.json")


def build_product(*, title: str, description: str, tags: list[str],
                  blueprint_id: int, print_provider_id: int, variant_ids: list[int],
                  price_dollars: str, position: str, image_id: str,
                  scale: float, x: float = 0.5, y: float = 0.5, angle: int = 0) -> dict:
    price_cents = int(round(float(price_dollars) * 100))
    variants = [{"id": int(v), "price": price_cents, "is_enabled": True} for v in variant_ids]
    return {
        "title": title,
        "description": description,
        "tags": tags,
        "blueprint_id": int(blueprint_id),
        "print_provider_id": int(print_provider_id),
        "variants": variants,
        "print_areas": [{
            "variant_ids": [int(v) for v in variant_ids],
            "placeholders": [{
                "position": position,
                "images": [{"id": image_id, "x": x, "y": y,
                            "scale": round(scale, 4), "angle": angle}],
            }],
        }],
    }
