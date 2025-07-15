import os
import re
import json
import requests
import logging
from flask import Flask, request, jsonify
from urllib.parse import urlencode

app = Flask(__name__)

LOG_LEVEL = os.environ.get("LOG_LEVEL", "WARNING").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL))
logger = logging.getLogger(__name__)

SOLR_BASE_URL = os.environ.get("SOLR_URL", "http://localhost:8983/solr")
SOLR_CORE = os.environ.get("SOLR_CORE", "texts")
LANG_CODE = os.environ.get("OCR_LANG_CODE", "en")

def escape_solr_term(term):
    # Basic escape for special chars in Solr query syntax
    return re.sub(r'([+\-&|!(){}\[\]^"~*?:\\/])', r'\\\1', term)

def convert_bbox_to_xywh(bbox_str):
    # bbox_str example: "2946 2016 3091 2055"
    try:
        x1, y1, x2, y2 = map(int, bbox_str.strip().split())
        width = x2 - x1
        height = y2 - y1
        return f"{x1},{y1},{width},{height}"
    except Exception as e:
        logger.error(f"Invalid bbox format: {bbox_str} - {e}")
        return None

def query_solr(q, uri, page, rows, object_id=None):
    query_word = escape_solr_term(q.lower())
    bbox_field = f"ocr_hitbox_{LANG_CODE}_tsm"

    solr_params = {
        "q": query_word,
        "defType": "edismax",
        "qf": f"ocr_text_{LANG_CODE}_tsimv",
        "rows": rows,
        "start": (page - 1) * rows,
        "wt": "json",
        "fl": f"id,canvas_id_ssi,{bbox_field}"
    }

    fq_clauses = []
    if uri:
        fq_clauses.append(f'canvas_id_ssi:"{uri}"')
    if object_id:
        fq_clauses.append(f'object_id_ssi:"{object_id}"')

    if fq_clauses:
        solr_params["fq"] = fq_clauses

    solr_url = f"{SOLR_BASE_URL}/{SOLR_CORE}/select"
    try:
        solr_resp = requests.get(solr_url, params=solr_params, timeout=10)
        solr_resp.raise_for_status()
        logger.debug(f"Solr URL: {solr_resp.url}")
        data = solr_resp.json()
        #logger.debug(json.dumps(data, indent=2))
        return data.get("response", {}).get("docs", []), data.get("response", {}).get("numFound", 0)
    except requests.RequestException as e:
        logger.error(f"Solr error: {e}")
        return None, None


@app.route("/search/1/<collection_id>/<object_id>", methods=["GET"])
def search_1(collection_id, object_id):
    q = request.args.get("q")
    uri = request.args.get("uri")
    page = int(request.args.get("page", 1))
    rows = int(request.args.get("rows", 50))
    start_index = (page - 1) * rows

    if not q:
        return jsonify({"error": "Missing required query parameter 'q'"}), 400

    docs, total = query_solr(q, uri, page, rows, object_id=f"{collection_id}/{object_id}")
    if docs is None:
        return jsonify({"error": "Solr unavailable"}), 503

    annotations = []
    hits = []
    bbox_field = f"ocr_hitbox_{LANG_CODE}_tsm"

    query_terms = [term.lower() for term in q.split()]
    for doc in docs:
        canvas_id = doc.get("canvas_id_ssi")
        if not canvas_id:
            continue

        hitboxes = doc.get(bbox_field, [])

        for idx, val in enumerate(hitboxes):
            try:
                word, bbox = val.split("|", 1)
                if not any(term in word.lower() for term in query_terms):
                    continue
                xywh = convert_bbox_to_xywh(bbox)
                if xywh is None:
                    continue
            except Exception:
                continue

            anno_id = f"{request.url_root.rstrip('/')}/annotation/{doc['id']}_{idx}"
            annotations.append({
                "@id": anno_id,
                "@type": "oa:Annotation",
                "motivation": "sc:painting",
                "resource": {
                    "@type": "cnt:ContentAsText",
                    "chars": word
                },
                "on": f"{canvas_id}#xywh={xywh}"
            })

            hits.append({
                "@type": "search:Hit",
                "annotations": [anno_id]
            })

    return jsonify({
        "@context": "http://iiif.io/api/search/0/context.json",
        "@id": request.url,
        "@type": "sc:AnnotationList",
        "startIndex": start_index,
        "within": {
            "ignored": [],
            "total": total,
            "@type": "sc:Layer"
        },
        "hits": hits,
        "resources": annotations
    })


# not fully implemented
@app.route("/search/2/<collection_id>/<object_id>", methods=["GET"])
def search_2():
    q = request.args.get("q")
    uri = request.args.get("uri")
    page = int(request.args.get("page", 1))
    rows = int(request.args.get("rows", 50))

    if not q:
        return jsonify({"error": "Missing required query parameter 'q'"}), 400

    docs, total = query_solr(q, uri, page, rows)
    if docs is None:
        return jsonify({"error": "Solr unavailable"}), 503

    hits = []
    annotations = []
    bbox_field = f"ocr_hitbox_{LANG_CODE}_tsm"

    for doc in docs:
        canvas_id = doc.get("canvas_id_ssi")
        if not canvas_id:
            continue

        hitboxes = doc.get(bbox_field, [])
        annos = []
        for idx, val in enumerate(hitboxes):
            try:
                word, bbox = val.split("|", 1)
                xywh = convert_bbox_to_xywh(bbox)
                if xywh is None:
                    continue
            except Exception:
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

    def with_page(p):
        q_args = request.args.copy()
        q_args["page"] = str(p)
        return f"{base_url}?{urlencode(q_args)}"

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

    if total > page * rows:
        response["within"]["next"] = with_page(page + 1)
    if page > 1:
        response["within"]["prev"] = with_page(page - 1)

    return jsonify(response)


@app.route("/annotation/<annotation_id>", methods=["GET"])
def get_annotation(annotation_id):
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


@app.route("/search/health", methods=["GET"])
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
