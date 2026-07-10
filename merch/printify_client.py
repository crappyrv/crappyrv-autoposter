"""
printify_client — create a product on Printify and publish it to Etsy as a draft.

Unlike Printful, Printify's API CAN create products on a connected Etsy store and
push them to Etsy. With the store's publishing set to "Manual approval", the
pushed product lands as an Etsy DRAFT (the review gate David wants).

Flow: upload_image(url) -> create_product(...) -> publish_product(id).
Printify requires a User-Agent header on every request.
"""
from __future__ import annotations
import requests

BASE = "https://api.printify.com/v1"


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
        r = requests.request(method, BASE + path, headers=self._headers(),
                             timeout=self.timeout, **kw)
        if r.status_code >= 400:
            raise PrintifyError(f"HTTP {r.status_code} on {method} {path}: {r.text[:400]}")
        return r.json() if r.text else {}

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
