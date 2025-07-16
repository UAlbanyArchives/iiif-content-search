import os
import re
import json
import string
import logging
import requests
from bs4 import BeautifulSoup
from urllib.parse import urlencode
from flask import Flask, request, jsonify

app = Flask(__name__)

LOG_LEVEL = os.environ.get("LOG_LEVEL", "WARNING").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL))
logger = logging.getLogger(__name__)

SOLR_BASE_URL = os.environ.get("SOLR_URL", "http://localhost:8983/solr")
SOLR_CORE = os.environ.get("SOLR_CORE", "texts")
LANG_CODE = os.environ.get("OCR_LANG_CODE", "en")

def normalize(term):
    return term.strip(string.punctuation).lower()

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

def query_solr_with_highlighting(q, uri, page, rows, object_id=None):
    bbox_field = f"ocr_hitbox_{LANG_CODE}_tsm"
    text_field = f"ocr_text_{LANG_CODE}_tsimv"

    solr_params = {
        "q": q,
        "defType": "edismax",
        "qf": text_field,
        "rows": rows,
        "start": (page - 1) * rows,
        "wt": "json",
        "fl": f"id,canvas_id_ssi,{bbox_field},{text_field}",
        "hl": "true",
        "hl.fl": text_field,
        "hl.simple.pre": "<em>",
        "hl.simple.post": "</em>",
        "hl.fragsize": 1000,  # large enough to get full context
        "hl.mergeContiguous": "true"
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
        data = solr_resp.json()
        return data
    except requests.RequestException as e:
        logger.error(f"Solr error: {e}")
        return None


@app.route("/search/1/<collection_id>/<object_id>", methods=["GET"])
def search_1(collection_id, object_id):
    q = request.args.get("q")
    uri = request.args.get("uri")
    page = int(request.args.get("page", 1))
    rows = int(request.args.get("rows", 50))
    start_index = (page - 1) * rows

    if not q:
        return jsonify({"error": "Missing required query parameter 'q'"}), 400

    solr_data = query_solr_with_highlighting(q, uri, page, rows, object_id=f"{collection_id}/{object_id}")
    if solr_data is None:
        return jsonify({"error": "Solr unavailable"}), 503

    docs = solr_data.get("response", {}).get("docs", [])
    total = solr_data.get("response", {}).get("numFound", 0)
    highlighting = solr_data.get("highlighting", {})

    annotations = []
    hits = []
    bbox_field = f"ocr_hitbox_{LANG_CODE}_tsm"
    text_field = f"ocr_text_{LANG_CODE}_tsimv"

    query_terms = [normalize(w) for w in q.split()]

    for doc in docs:
        doc_id = doc.get("id")
        canvas_id = doc.get("canvas_id_ssi")
        if not canvas_id or doc_id not in highlighting:
            continue

        hitboxes = doc.get(bbox_field, [])
        doc_text = doc.get(text_field, "")

        # Normalize all hitboxes for matching
        normalized_hitboxes = [
            (normalize(val.split("|", 1)[0]), val.split("|", 1)[1])
            for val in hitboxes if "|" in val
        ]

        # Get the highlighted snippets for this doc
        highlight_snippets = highlighting.get(doc_id, {}).get(text_field, [])

        for snippet in highlight_snippets:
            # snippet contains <em>...</em> around matched terms/phrases
            # Remove html tags but keep <em> markers for matching
            # Split snippet by whitespace, noting which words are in <em>...</em>

            # Parse snippet as HTML to detect <em> tags
            soup = BeautifulSoup(snippet, "html.parser")
            words_with_em = []
            for elem in soup.recursiveChildGenerator():
                if isinstance(elem, str):
                    # split text by whitespace
                    for w in elem.split():
                        words_with_em.append((normalize(w), False))
                elif elem.name == "em":
                    # highlighted word(s)
                    highlighted_text = elem.get_text(" ", strip=True)
                    for w in highlighted_text.split():
                        words_with_em.append((normalize(w), True))

            # Identify continuous spans of highlighted words (the phrase matches)
            # We'll find runs where em=True for consecutive words

            idx = 0
            while idx < len(words_with_em):
                if words_with_em[idx][1]:
                    # Start of phrase match
                    start_idx = idx
                    while idx < len(words_with_em) and words_with_em[idx][1]:
                        idx += 1
                    end_idx = idx  # exclusive

                    phrase_words = [w for w, _ in words_with_em[start_idx:end_idx]]

                    # Find this phrase sequence in normalized_hitboxes
                    # Naive approach: scan hitboxes for exact phrase match

                    for i in range(len(normalized_hitboxes) - len(phrase_words) + 1):
                        window = normalized_hitboxes[i:i+len(phrase_words)]
                        window_words = [w for w, _ in window]
                        if window_words == phrase_words:
                            # Combine bounding boxes into one
                            bboxes = [w[1] for w in window]
                            xywhs = [convert_bbox_to_xywh(b) for b in bboxes]
                            # Skip if any invalid bbox
                            if None in xywhs:
                                continue

                            # Calculate union bbox (smallest rectangle containing all)
                            xs = []
                            ys = []
                            xe = []
                            ye = []
                            for b in bboxes:
                                x1, y1, x2, y2 = map(int, b.split())
                                xs.append(x1)
                                ys.append(y1)
                                xe.append(x2)
                                ye.append(y2)

                            union_x1 = min(xs)
                            union_y1 = min(ys)
                            union_x2 = max(xe)
                            union_y2 = max(ye)
                            width = union_x2 - union_x1
                            height = union_y2 - union_y1
                            xywh = f"{union_x1},{union_y1},{width},{height}"

                            # Create annotation for phrase
                            anno_id = f"{request.url_root.rstrip('/')}/annotation/{doc_id}_{i}_{i+len(phrase_words)-1}"
                            annotations.append({
                                "@id": anno_id,
                                "@type": "oa:Annotation",
                                "motivation": "sc:painting",
                                "resource": {
                                    "@type": "cnt:ContentAsText",
                                    "chars": " ".join(phrase_words)
                                },
                                "on": f"{canvas_id}#xywh={xywh}"
                            })

                            hits.append({
                                "@type": "search:Hit",
                                "annotations": [anno_id]
                            })
                            break  # phrase found, no need to find again in this snippet
                else:
                    idx += 1

    return jsonify({
        "@context": "http://iiif.io/api/search/1/context.json",
        "@id": request.url,
        "@type": "AnnotationPage",
        "startIndex": start_index,
        "within": {
            "ignored": [],
            "total": total,
            "@type": "sc:Layer"
        },
        "hits": hits,
        "resources": annotations
    })


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
