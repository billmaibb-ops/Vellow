"""
cj_client.py — Minimal CJ Dropshipping API v2 client.

Docs: https://developers.cjdropshipping.com/  (API 2.0)

What this covers:
  - Authentication (getAccessToken via apiKey) with token caching + refresh
  - Product search + single-product query (price, images, description)
  - Variant stock/inventory lookup (for the hourly lightweight poll)
  - Order creation (forward a customer order to CJ for fulfillment)

IMPORTANT: endpoint paths and field names are based on CJ API 2.0 as of
mid-2026. CJ changes these occasionally — if a call 4xx's, open your CJ
developer dashboard and diff the request against the live docs. The auth
header CJ expects is `CJ-Access-Token`.
"""

import json
import os
import time
import threading
from pathlib import Path

import requests

BASE = "https://developers.cjdropshipping.com/api2.0/v1"

# Where we persist the access token so we don't re-auth every run.
# CJ rate-limits getAccessToken (roughly 1 call / 5 min), so caching is required.
TOKEN_CACHE = Path(__file__).with_name(".cj_token.json")


class CJError(RuntimeError):
    """Raised when CJ returns a non-success payload."""


class CJClient:
    def __init__(self, api_key: str | None = None, timeout: int = 30):
        self.api_key = api_key or os.environ["CJ_API_KEY"]  # "CJUserNum@api@xxxx"
        self.timeout = timeout
        self._lock = threading.Lock()
        self._token = None
        self._token_exp = 0.0
        self._load_cached_token()

    # ---------------- auth ----------------
    def _load_cached_token(self):
        if TOKEN_CACHE.exists():
            try:
                data = json.loads(TOKEN_CACHE.read_text())
                self._token = data.get("accessToken")
                # store expiry as epoch seconds; refresh 5 min early
                self._token_exp = data.get("_exp_epoch", 0.0)
            except Exception:
                pass

    def _save_cached_token(self, access_token: str, exp_epoch: float):
        TOKEN_CACHE.write_text(json.dumps(
            {"accessToken": access_token, "_exp_epoch": exp_epoch}))
        try:
            TOKEN_CACHE.chmod(0o600)  # token is a secret
        except Exception:
            pass

    def _authenticate(self):
        url = f"{BASE}/authentication/getAccessToken"
        r = requests.post(url, json={"apiKey": self.api_key}, timeout=self.timeout)
        r.raise_for_status()
        payload = r.json()
        if not payload.get("result", True) and payload.get("code") not in (200, None):
            raise CJError(f"CJ auth failed: {payload}")
        data = payload["data"]
        self._token = data["accessToken"]
        # CJ returns accessTokenExpiryDate (ISO). We conservatively cache for 6h.
        self._token_exp = time.time() + 6 * 3600
        self._save_cached_token(self._token, self._token_exp)

    def _token_valid(self) -> bool:
        return bool(self._token) and time.time() < (self._token_exp - 300)

    def _headers(self) -> dict:
        with self._lock:
            if not self._token_valid():
                self._authenticate()
        return {"CJ-Access-Token": self._token, "Content-Type": "application/json"}

    # ---------------- low-level request w/ one retry on 401 ----------------
    def _request(self, method: str, path: str, *, params=None, json_body=None) -> dict:
        """One CJ request with retries for auth (401) and rate-limit (429).

        CJ rate-limits product endpoints hard; on 429 we honor Retry-After
        (or back off exponentially) and retry, so a big deep sync rides through
        throttling instead of crashing."""
        backoff = 3.0
        for attempt in range(1, 7):
            r = requests.request(method, f"{BASE}{path}", params=params,
                                 json=json_body, headers=self._headers(),
                                 timeout=self.timeout)
            if r.status_code == 401 and attempt < 6:
                with self._lock:
                    self._token = None          # force re-auth
                continue
            if r.status_code == 429 and attempt < 6:
                wait = float(r.headers.get("Retry-After", 0)) or backoff
                time.sleep(min(wait, 30))
                backoff = min(backoff * 2, 30)
                continue
            # Non-2xx: surface CJ's actual error body as a CJError (callers
            # catch CJError) instead of a raw HTTPError that would 500 the app.
            if r.status_code >= 400:
                try:
                    eb = r.json()
                    detail = eb.get("message") or eb
                except Exception:
                    detail = r.text[:300]
                raise CJError(f"CJ {r.status_code} on {path}: {detail}")
            body = r.json()
            # CJ signals its own rate limit in-body too ("Too Many Requests").
            msg = str(body.get("message") or "")
            if body.get("code") == 429 or "many request" in msg.lower():
                if attempt < 6:
                    time.sleep(backoff); backoff = min(backoff * 2, 30); continue
            if body.get("code") not in (200, None) and body.get("result") is False:
                raise CJError(f"CJ error on {path}: {body.get('message')} ({body})")
            return body
        raise CJError(f"CJ request failed after retries: {path}")

    def _get(self, path: str, params: dict) -> dict:
        return self._request("GET", path, params=params)

    def _post(self, path: str, data: dict) -> dict:
        return self._request("POST", path, json_body=data)

    # ---------------- products ----------------
    def search_products(self, keyword: str, page: int = 1, size: int = 20) -> list[dict]:
        """Search CJ catalog. Use to discover products to list. (Daily / manual.)"""
        body = self._get("/product/listV2",
                         {"pageNum": page, "pageSize": size, "keyWord": keyword})
        return (body.get("data") or {}).get("list", [])

    def list_products(self, page: int = 1, size: int = 40,
                      keyword: str | None = None,
                      category_id: str | None = None,
                      country_code: str | None = None) -> dict:
        """Browse the FULL CJ catalog, paginated. No keyword = everything.

        Returns {'list': [...], 'total': int} so the caller can paginate
        through the whole catalog (CJ caps pageSize at ~200).
        """
        params: dict = {"pageNum": page, "pageSize": size}
        if keyword:
            params["keyWord"] = keyword
        if category_id:
            params["categoryId"] = category_id
        if country_code:                      # e.g. "US" -> US-warehouse stock only
            params["countryCode"] = country_code
        body = self._get("/product/listV2", params)
        data = body.get("data") or {}
        return {"list": data.get("list", []),
                "total": int(data.get("total") or 0)}

    def get_categories(self) -> list[dict]:
        """Full CJ category tree (3 levels). Cache this — it rarely changes."""
        body = self._get("/product/getCategory", {})
        return body.get("data") or []

    def get_product(self, pid: str) -> dict:
        """Full product detail: title, images, description, variants. (Daily deep sync.)"""
        body = self._get("/product/query", {"pid": pid})
        return body.get("data") or {}

    def get_variant_stock(self, vid: str) -> dict:
        """Lightweight inventory lookup for one variant. (Hourly poll.)

        Returns a dict with at least: {'quantity': int, 'available': bool}.
        CJ exposes inventory per-variant; we normalize the fields we need.
        """
        body = self._get("/product/stock/queryByVid", {"vid": vid})
        data = body.get("data") or {}
        # CJ returns a list of warehouse stocks; sum US-warehouse quantity.
        rows = data if isinstance(data, list) else data.get("list", [data])
        total = 0
        us_total = 0
        for row in rows:
            qty = int(row.get("storageNum") or row.get("quantity") or 0)
            total += qty
            area = (row.get("areaEn") or row.get("countryCode") or "").upper()
            if area in ("US", "USA", "UNITED STATES"):
                us_total += qty
        return {"quantity": total, "us_quantity": us_total, "available": total > 0}

    # ---------------- orders ----------------
    def get_tracking(self, order_id: str) -> dict | None:
        """Look up shipment tracking for a placed CJ order. Returns
        {"tracking": str, "carrier": str} once available, else None.
        Tries a couple of CJ endpoints defensively since field names vary."""
        if not order_id:
            return None
        for path, params in (
            ("/shopping/order/getOrderDetail", {"orderId": order_id}),
            ("/logistic/getTrackInfo", {"orderId": order_id}),
        ):
            try:
                body = self._get(path, params)
            except CJError:
                continue
            data = body.get("data") or {}
            rows = data if isinstance(data, list) else [data]
            for d in rows:
                tn = (d.get("trackNumber") or d.get("trackingNumber")
                      or d.get("trackNo") or d.get("logisticNumber"))
                if tn:
                    return {"tracking": str(tn),
                            "carrier": d.get("logisticName") or d.get("trackingName")
                                       or d.get("carrier") or ""}
        return None

    def create_order(self, order: dict) -> dict:
        """Forward a paid order to CJ for fulfillment.

        `order` must include the customer's shipping address and the CJ
        variant IDs + quantities. Shape follows CJ's createOrderV2.
        Only call this AFTER you have captured payment.

        NOTE: createOrderV2 only CREATES the order in CJ — it does not pay it.
        CJ holds the order as an unpaid draft until you pay it from your CJ
        wallet balance (see pay_order). Nothing ships until the order is paid.
        """
        body = self._post("/shopping/order/createOrderV2", order)
        return body.get("data") or {}

    def get_balance(self) -> dict:
        """Read the CJ wallet balance. Pure read (no money moves).

        Returns {"amount": float, "raw": <cj payload>} on success. CJ field
        names vary by account/version, so we probe a couple of endpoints and
        normalize the first numeric balance we find. Raises CJError if none
        of the endpoints answer."""
        last_err = None
        for path in ("/shopping/pay/getBalance", "/shopping/pay/queryBalance"):
            try:
                body = self._get(path, {})
            except CJError as e:
                last_err = e
                continue
            data = body.get("data")
            if data is None:
                continue
            rows = data if isinstance(data, list) else [data]
            for d in rows:
                if not isinstance(d, dict):
                    try:               # some accounts return the number directly
                        return {"amount": float(d), "raw": data}
                    except (TypeError, ValueError):
                        continue
                for k in ("amount", "balance", "totalAmount",
                          "availableBalance", "money"):
                    if d.get(k) is not None:
                        try:
                            return {"amount": float(d[k]), "raw": data}
                        except (TypeError, ValueError):
                            pass
            return {"amount": 0.0, "raw": data}
        raise CJError(f"Could not read CJ balance: {last_err}")

    def pay_order(self, order_id: str, order_number: str | None = None) -> dict:
        """Pay a created CJ order from the wallet balance so CJ fulfills it.

        SPENDS REAL MONEY from your CJ wallet. Only call after create_order
        and after the customer's payment has been captured. `order_id` is the
        `orderId` returned by create_order; `order_number` is the merchant
        orderNumber you passed to create_order (some CJ accounts key the
        pay endpoint on one or the other).

        The endpoint path (/shopping/pay/payBalance) is confirmed, but CJ can
        (a) key on orderId vs orderNumber and (b) briefly 'not find' an order
        that was just created (replication lag). So we try each id field and
        retry a couple of times on a 'not found' response before giving up.

        Returns CJ's response dict. Raises CJError on real failure (e.g. an
        unfunded wallet's 'insufficient balance', which is a safe signal that
        the request shape is correct and only funding is missing — no money
        moves in that case)."""
        if not order_id and not order_number:
            raise CJError("pay_order: missing order id/number")
        # Candidate payloads, most-likely first.
        candidates = []
        if order_id:
            candidates.append({"orderId": order_id})
            candidates.append({"orderIds": [order_id]})
        if order_number:
            candidates.append({"orderNumber": order_number})
        last = None
        for payload in candidates:
            for attempt in range(3):
                try:
                    body = self._post("/shopping/pay/payBalance", payload)
                    return body.get("data") or body
                except CJError as e:
                    last = e
                    # Retry only the 'not found' case (likely propagation lag);
                    # any other error (e.g. insufficient balance) is definitive
                    # for this payload, so move to the next candidate.
                    if "not found" in str(e).lower() and attempt < 2:
                        time.sleep(3)
                        continue
                    break
        raise CJError(f"payBalance failed: {last}")

    def get_shipping_quote(self, vid: str, quantity: int, country: str, zip_code: str) -> dict:
        """Real carrier/shipping cost for a variant to a destination.
        Use at checkout to show the grossed-up shipping line before capture."""
        body = self._post("/logistic/freightCalculate", {
            "startCountryCode": "US",
            "endCountryCode": country,
            "zip": zip_code,
            "products": [{"vid": vid, "quantity": quantity}],
        })
        return body.get("data") or {}

    def get_shipping_quote_multi(self, products: list[dict], country: str,
                                 zip_code: str, province: str = "") -> dict:
        """Live CJ shipping cost for a whole cart to the customer's address.

        `products` = [{"vid": ..., "quantity": ...}, ...]. Returns the
        CHEAPEST available logistics option:
            {"cost": float, "name": str, "days": str, "options": int}
        Raises CJError if CJ returns nothing usable so the caller can fall
        back rather than charge a wrong shipping amount.
        """
        body = self._post("/logistic/freightCalculate", {
            "startCountryCode": "US",
            "endCountryCode": country,
            "zip": zip_code,
            "province": province,
            "products": products,
        })
        data = body.get("data") or []
        rows = data if isinstance(data, list) else data.get("list", [data])
        best = None
        for r in rows:
            raw = (r.get("logisticPrice") or r.get("freight")
                   or r.get("price") or r.get("amount"))
            try:
                cost = float(raw)
            except (TypeError, ValueError):
                continue
            if best is None or cost < best["cost"]:
                best = {
                    "cost": round(cost, 2),
                    "name": r.get("logisticName") or r.get("logisticEnName")
                            or r.get("name") or "Standard shipping",
                    "days": str(r.get("logisticAging") or r.get("aging")
                               or r.get("deliveryTime") or ""),
                    "options": len(rows),
                }
        if best is None:
            raise CJError(f"No shipping options returned for {country}/{zip_code}")
        return best


if __name__ == "__main__":
    # Smoke test (requires CJ_API_KEY in env). Prints the first search hit.
    cj = CJClient()
    hits = cj.search_products("phone ring holder", size=3)
    print(f"Got {len(hits)} results")
    for h in hits[:3]:
        print(" -", h.get("productNameEn"), "| pid:", h.get("pid"))
