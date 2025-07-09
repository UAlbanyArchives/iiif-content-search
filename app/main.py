import os
import json
import requests
import logging
from flask import Flask, request, jsonify
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

app = Flask(__name__)

LOG_LEVEL = os.environ.get("LOG_LEVEL", "WARNING").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL))
logger = logging.getLogger(__name__)

SOLR_BASE_URL = os.environ.get("SOLR_URL", "http://localhost:8983/solr")
SOLR_CORE = os.environ.get("SOLR_CORE", "texts")
LANG_CODE = os.environ.get("OCR_LANG_CODE", "en")

@app.route("/search", methods=["GET"])
def search():
    q = request.args.get("q")
    uri = request.args.get("uri")
    page = int(request.args.get("page", 1))
    rows = int(request.args.get("rows", 50))

    if not q:
        return jsonify({"error": "Missing required query parameter 'q'"}), 400

    # Use language-specific field with termVectors enabled
    text_field = f"ocr_text_{LANG_CODE}_tsimv"
    bbox_field = f"ocr_hitbox_{LANG_CODE}_tsm"

    fq = [f"{text_field}:{q}"]
    if uri:
        fq.append(f"canvas_id_ssi:\"{uri}\"")

    solr_params = {
        "q": f"{text_field}:{q}",
        "fq": fq,
        "rows": rows,
        "start": (page - 1) * rows,
        "wt": "json",
        "fl": f"id,canvas_id_ssi,{text_field},{bbox_field}"
    }

    solr_url = f"{SOLR_BASE_URL}/{SOLR_CORE}/select"

    try:
        solr_resp = requests.get(solr_url, params=solr_params, timeout=10)
        solr_resp.raise_for_status()
    except requests.RequestException as e:
        logger.error(f"Solr error: {e}")
        return jsonify({"error": "Solr unavailable"}), 503

    docs = solr_resp.json().get("response", {}).get("docs", [])
    total = solr_resp.json().get("response", {}).get("numFound", 0)

    hits = []
    annotations = []

    for doc in docs:
        canvas_id = doc.get("canvas_id_ssi")
        if not canvas_id:
            continue

        hitboxes = doc.get(bbox_field, [])

        annos = []
        for idx, val in enumerate(hitboxes):
            try:
                word, bbox = val.split("|", 1)
                xywh = bbox.replace(" ", ",")
            except ValueError:
                continue

            anno_id = f"{request.url_root.rstrip('/')}/annotation/{doc['id']}_{idx}"
            annotations.append({
                "id": anno_id,
                "type": "Annotation",
                "motivation": "highlighting",
                "target": f"{canvas_id}#xywh={xywh}",
                "body": {
                    "type": "TextualBody",
                    "value": word,
                    "format": "text/plain"
                }
            })
            annos.append(anno_id)

        hits.append({
            "type": "Hit",
            "annotations": annos,
            "match": q,
            "before": "",
            "after": ""
        })

    base_url = request.base_url
    query_args = request.args.to_dict()

    response = {
        "@context": "http://iiif.io/api/search/2/context.json",
        "id": request.url,
        "type": "AnnotationCollection",
        "within": {
            "type": "OrderedCollection",
            "total": total
        },
        "resources": annotations,
        "hits": hits
    }

    # Pagination
    def with_page(p):
        q = request.args.copy()
        q["page"] = str(p)
        return f"{base_url}?{urlencode(q)}"

    if total > page * rows:
        response["within"]["next"] = with_page(page + 1)
    if page > 1:
        response["within"]["prev"] = with_page(page - 1)

    return jsonify(response)


@app.route("/annotation/<annotation_id>", methods=["GET"])
def get_annotation(annotation_id):
    # Not persisted, so return minimal example
    return jsonify({
        "id": f"{request.url_root.rstrip('/')}/annotation/{annotation_id}",
        "type": "Annotation",
        "motivation": "highlighting",
        "body": {
            "type": "TextualBody",
            "value": "Annotation text",
            "format": "text/plain"
        }
    })


@app.route("/health", methods=["GET"])
def health_check():
    try:
        solr_url = f"{SOLR_BASE_URL}/{SOLR_CORE}/select"
        response = requests.get(solr_url, params={"q": "*:*", "rows": 0}, timeout=5)
        response.raise_for_status()
        return jsonify({
            "status": "healthy",
            "solr": "connected",
            "solr_url": SOLR_BASE_URL,
            "solr_core": SOLR_CORE
        })
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return jsonify({"status": "unhealthy", "error": str(e)}), 503


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
