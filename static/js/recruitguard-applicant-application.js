/*
 * Applicant application page (redesigned intake, step 3).
 *
 *   - Each document card takes several files. Every file uploads on its own, the moment it
 *     is picked, with its own progress bar; a dropped connection only costs that one file.
 *   - Large photos are shrunk in the browser first (long edge 2400px, JPEG), which also
 *     drops phone metadata such as GPS location. The server cleans photos again anyway.
 *   - Typed answers autosave (debounced) to the same page with action=autosave.
 *   - "x of y required documents ready" and the "still needed" list update live.
 *
 * The server is the authority for every rule (types, sizes, duplicates, required
 * documents); this script only makes the waiting and the mistakes friendlier.
 */
(function () {
    "use strict";

    var root = document.querySelector("[data-application]");
    var form = document.getElementById("rg-application-form");
    if (!root || !form) return;

    var MAX_BYTES = parseInt(root.getAttribute("data-max-upload-bytes"), 10) || 5 * 1024 * 1024;
    var MAX_MB = root.getAttribute("data-max-upload-mb") || "5";
    var SHRINK_OVER_BYTES = 1.5 * 1024 * 1024;
    var MAX_EDGE = 2400;
    var ALLOWED = { "application/pdf": "pdf", "image/jpeg": "jpg", "image/png": "png" };
    var csrf = (form.querySelector("input[name=csrfmiddlewaretoken]") || {}).value || "";

    /* Messages in the page's language (English / Filipino), from the server as JSON.
       t(key, fallback, {name: value}) fills {name} placeholders. */
    var STRINGS = {};
    try { STRINGS = JSON.parse(document.getElementById("rg-apply-i18n").textContent); } catch (x) {}
    function t(key, fallback, values) {
        var text = STRINGS[key] || fallback;
        Object.keys(values || {}).forEach(function (name) { text = text.split("{" + name + "}").join(values[name]); });
        return text;
    }

    /* ── helpers ─────────────────────────────────────────────── */
    function formatSize(bytes) {
        if (bytes < 1024) return bytes + " B";
        if (bytes < 1024 * 1024) return Math.round(bytes / 1024) + " KB";
        return (Math.round(bytes / 1024 / 1024 * 10) / 10) + " MB";
    }
    function partUrl(template, id) {
        return template.replace(/\/0\/(delete\/)?$/, "/" + id + "/$1");
    }
    function kindOf(file) {
        if (ALLOWED[file.type]) return ALLOWED[file.type];
        var ext = String(file.name || "").split(".").pop().toLowerCase();
        if (ext === "pdf") return "pdf";
        if (ext === "jpg" || ext === "jpeg") return "jpg";
        if (ext === "png") return "png";
        return null;
    }

    /* Shrink a photo in the browser when it is big. Resolves to a File (maybe the original). */
    function shrinkIfPhoto(file) {
        var kind = kindOf(file);
        if (kind !== "jpg" && kind !== "png") return Promise.resolve(file);
        if (file.size <= SHRINK_OVER_BYTES || !window.createImageBitmap) return Promise.resolve(file);
        return createImageBitmap(file, { imageOrientation: "from-image" }).then(function (bitmap) {
            var scale = Math.min(1, MAX_EDGE / Math.max(bitmap.width, bitmap.height));
            var canvas = document.createElement("canvas");
            canvas.width = Math.round(bitmap.width * scale);
            canvas.height = Math.round(bitmap.height * scale);
            canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
            if (bitmap.close) bitmap.close();
            return new Promise(function (resolve) {
                canvas.toBlob(function (blob) {
                    if (!blob || blob.size >= file.size) { resolve(file); return; }
                    var name = String(file.name || "photo").replace(/\.(png|jpe?g)$/i, "") + ".jpg";
                    resolve(new File([blob], name, { type: "image/jpeg" }));
                }, "image/jpeg", 0.85);
            });
        }).catch(function () { return file; });
    }

    /* Photo quality check (after upload, never blocks): very small, dark or blurry photos of
       documents are the most common reason the office asks for a document again. */
    var QUALITY_MIN_EDGE = 1000;   // px on the long edge
    var QUALITY_MIN_BRIGHTNESS = 70; // average luminance, 0-255
    var QUALITY_MIN_SHARPNESS = 40;  // variance of the Laplacian on a 320px-wide copy
    function checkPhotoQuality(file) {
        var kind = kindOf(file);
        if ((kind !== "jpg" && kind !== "png") || !window.createImageBitmap) return Promise.resolve([]);
        return createImageBitmap(file, { imageOrientation: "from-image" }).then(function (bitmap) {
            var issues = [];
            if (Math.max(bitmap.width, bitmap.height) < QUALITY_MIN_EDGE) issues.push("small");
            var width = Math.min(320, bitmap.width);
            var height = Math.max(1, Math.round(bitmap.height * (width / bitmap.width)));
            var canvas = document.createElement("canvas");
            canvas.width = width;
            canvas.height = height;
            var ctx = canvas.getContext("2d");
            ctx.drawImage(bitmap, 0, 0, width, height);
            if (bitmap.close) bitmap.close();
            var pixels = ctx.getImageData(0, 0, width, height).data;
            var gray = new Float32Array(width * height);
            var total = 0;
            for (var i = 0, p = 0; i < pixels.length; i += 4, p++) {
                gray[p] = 0.299 * pixels[i] + 0.587 * pixels[i + 1] + 0.114 * pixels[i + 2];
                total += gray[p];
            }
            if (total / gray.length < QUALITY_MIN_BRIGHTNESS) issues.push("dark");
            var sum = 0, sumSq = 0, count = 0;
            for (var y = 1; y < height - 1; y++) {
                for (var x = 1; x < width - 1; x++) {
                    var c = y * width + x;
                    var lap = gray[c - 1] + gray[c + 1] + gray[c - width] + gray[c + width] - 4 * gray[c];
                    sum += lap; sumSq += lap * lap; count++;
                }
            }
            if (count && (sumSq / count - Math.pow(sum / count, 2)) < QUALITY_MIN_SHARPNESS) issues.push("blurry");
            return issues;
        }).catch(function () { return []; });
    }
    var QUALITY_MESSAGES = {
        small: t("photo_small", "This photo is small, so the text may be hard to read."),
        dark: t("photo_dark", "This photo looks dark."),
        blurry: t("photo_blurry", "This photo looks blurry.")
    };
    function cardWarning(card, name, message) {
        var box = card.querySelector("[data-doc-warning]");
        if (!box || !message) return;
        var line = document.createElement("p");
        line.className = "rg-doc-card__error-line";
        var strong = document.createElement("strong");
        strong.textContent = name;
        line.appendChild(strong);
        line.appendChild(document.createTextNode(" — " + message));
        box.appendChild(line);
        box.hidden = false;
    }
    function qualityWarning(card, name, issues) {
        var box = card.querySelector("[data-doc-warning]");
        if (!box || !issues.length) return;
        var line = document.createElement("p");
        line.className = "rg-doc-card__error-line";
        var strong = document.createElement("strong");
        strong.textContent = name;
        line.appendChild(strong);
        line.appendChild(document.createTextNode(" — " + issues.map(function (i) { return QUALITY_MESSAGES[i]; }).join(" ") +
            " " + t("photo_saved_check", "It was saved. Tap View to check you can read it, and take it again if not.")));
        box.appendChild(line);
        box.hidden = false;
    }

    /* ── document cards ──────────────────────────────────────── */
    var partTemplate = document.getElementById("rg-part-template");
    var uploadingTemplate = document.getElementById("rg-part-uploading-template");

    /* Errors show one per line: the file's name, then a full sentence. */
    function fileError(name, message) { return { name: name, message: message }; }
    function cardError(card, errors) {
        var box = card.querySelector("[data-doc-error]");
        if (!box) return;
        if (typeof errors === "string") errors = errors ? [fileError("", errors)] : [];
        box.textContent = "";
        (errors || []).forEach(function (err) {
            var line = document.createElement("p");
            line.className = "rg-doc-card__error-line";
            if (err.name) {
                var strong = document.createElement("strong");
                strong.textContent = err.name;
                line.appendChild(strong);
                line.appendChild(document.createTextNode(" — "));
            }
            line.appendChild(document.createTextNode(err.message));
            box.appendChild(line);
        });
        box.hidden = !box.childNodes.length;
    }

    function renderPart(part) {
        var li = partTemplate.content.firstElementChild.cloneNode(true);
        li.setAttribute("data-part-id", part.id);
        li.querySelector(".rg-doc-part__icon").textContent = part.content_type === "application/pdf" ? "PDF" : "IMG";
        li.querySelector("[data-part-name]").textContent = part.filename;
        li.querySelector("[data-part-size]").textContent = formatSize(part.size_bytes);
        li.querySelector("[data-part-remove]").setAttribute("aria-label", t("remove_named", "Remove {name}", { name: part.filename }));
        return li;
    }

    function applySlotState(card, state) {
        var list = card.querySelector("[data-part-list]");
        list.querySelectorAll(".rg-doc-part:not(.is-uploading)").forEach(function (li) { li.remove(); });
        var firstUploading = list.querySelector(".is-uploading");
        state.parts.forEach(function (part) { list.insertBefore(renderPart(part), firstUploading); });
        var legacy = card.querySelector("[data-legacy-document]");
        if (legacy && state.parts.length) legacy.remove();
        card.classList.toggle("is-ready", !!state.is_ready);
        var status = card.querySelector("[data-doc-status]");
        if (status) {
            status.textContent = state.is_ready ? "✓ " + t("ready", "Ready")
                : (card.getAttribute("data-required") === "true" ? t("required", "Required") : t("if_applicable", "If applicable"));
        }
        var addLabel = card.querySelector(".rg-doc-card__add");
        if (addLabel) {
            var input = addLabel.querySelector("input");
            addLabel.textContent = state.parts.length ? t("add_another", "Add another file") : t("add_files", "Add files");
            addLabel.insertBefore(input, addLabel.firstChild);
        }
        updateProgress();
    }

    function uploadOne(card, file) {
        var row = uploadingTemplate.content.firstElementChild.cloneNode(true);
        row.querySelector("[data-part-name]").textContent = file.name;
        var bar = row.querySelector("progress");
        card.querySelector("[data-part-list]").appendChild(row);

        return shrinkIfPhoto(file).then(function (prepared) {
            if (prepared.size > MAX_BYTES) {
                row.remove();
                throw fileError(file.name, t("too_large", "This file is larger than {mb} MB. If it has several pages, " +
                    "add the pages one at a time, or take a photo of each page instead.", { mb: MAX_MB }));
            }
            return new Promise(function (resolve, reject) {
                var data = new FormData();
                data.append("file", prepared, prepared.name);
                var xhr = new XMLHttpRequest();
                xhr.open("POST", card.getAttribute("data-upload-url"));
                xhr.setRequestHeader("X-CSRFToken", csrf);
                xhr.setRequestHeader("X-Requested-With", "XMLHttpRequest");
                xhr.upload.addEventListener("progress", function (e) {
                    if (e.lengthComputable) bar.value = Math.round((e.loaded / e.total) * 100);
                });
                xhr.addEventListener("load", function () {
                    row.remove();
                    var body = null;
                    try { body = JSON.parse(xhr.responseText); } catch (x) {}
                    if (xhr.status === 200 && body) {
                        applySlotState(card, body);
                        if (body.warning) cardWarning(card, file.name, body.warning);
                        checkPhotoQuality(prepared).then(function (issues) { qualityWarning(card, file.name, issues); });
                        resolve();
                        return;
                    }
                    reject(fileError(file.name, (body && body.error) || t("upload_failed", "The upload did not go through. Try again.")));
                });
                xhr.addEventListener("error", function () {
                    row.remove();
                    reject(fileError(file.name, t("upload_offline", "The upload did not go through. Check your connection and try again.")));
                });
                xhr.send(data);
            });
        });
    }

    function handleFiles(card, files) {
        cardError(card, "");
        var warningBox = card.querySelector("[data-doc-warning]");
        if (warningBox) { warningBox.textContent = ""; warningBox.hidden = true; }
        var errors = [];
        var chain = Promise.resolve();
        Array.prototype.forEach.call(files, function (file) {
            if (!kindOf(file)) {
                errors.push(fileError(file.name, t("wrong_type", "This needs to be a PDF, JPG, or PNG file.")));
                return;
            }
            chain = chain.then(function () {
                return uploadOne(card, file).catch(function (err) {
                    errors.push(err && !(err instanceof Error) && err.message
                        ? err : fileError(file.name, t("upload_failed", "The upload did not go through. Try again.")));
                });
            });
        });
        chain.then(function () { cardError(card, errors); });
    }

    root.querySelectorAll("[data-doc-card]").forEach(function (card) {
        card.querySelectorAll("[data-part-input]").forEach(function (input) {
            input.addEventListener("change", function () {
                if (input.files && input.files.length) handleFiles(card, input.files);
                input.value = "";
            });
        });
    });

    /* "Use this file" from the applicant's earlier application. */
    root.addEventListener("click", function (e) {
        var reuseBtn = e.target.closest && e.target.closest("[data-reuse-url]");
        if (!reuseBtn) return;
        var card = reuseBtn.closest("[data-doc-card]");
        var data = new FormData();
        data.append("evidence_id", reuseBtn.getAttribute("data-evidence-id"));
        reuseBtn.disabled = true;
        reuseBtn.textContent = t("adding", "Adding…");
        fetch(reuseBtn.getAttribute("data-reuse-url"), {
            method: "POST",
            body: data,
            credentials: "same-origin",
            headers: { "X-CSRFToken": csrf, "X-Requested-With": "XMLHttpRequest" }
        }).then(function (res) { return res.json().then(function (body) { return [res, body]; }); })
          .then(function (pair) {
              if (pair[0].ok) {
                  applySlotState(card, pair[1]);
                  cardError(card, "");
                  var box = reuseBtn.closest("[data-reuse-box]");
                  if (box) box.remove();
              } else {
                  reuseBtn.disabled = false;
                  reuseBtn.textContent = t("use_this_file", "Use this file");
                  cardError(card, pair[1].error || t("add_failed", "That file could not be added."));
              }
          })
          .catch(function () {
              reuseBtn.disabled = false;
              reuseBtn.textContent = t("use_this_file", "Use this file");
              cardError(card, t("add_offline", "That file could not be added. Check your connection."));
          });
    });

    /* Remove / view (delegated, so freshly added rows work too). */
    var deleteTemplate = root.getAttribute("data-part-delete-url");
    var previewTemplate = root.getAttribute("data-part-preview-url");
    root.addEventListener("click", function (e) {
        var removeBtn = e.target.closest && e.target.closest("[data-part-remove]");
        var viewBtn = e.target.closest && e.target.closest("[data-part-view]");
        if (!removeBtn && !viewBtn) return;
        var li = e.target.closest("[data-part-id]");
        var card = e.target.closest("[data-doc-card]");
        var id = li && li.getAttribute("data-part-id");
        if (!id) return;
        if (removeBtn) {
            removeBtn.disabled = true;
            fetch(partUrl(deleteTemplate, id), {
                method: "POST",
                credentials: "same-origin",
                headers: { "X-CSRFToken": csrf, "X-Requested-With": "XMLHttpRequest" }
            }).then(function (res) { return res.json().then(function (body) { return [res, body]; }); })
              .then(function (pair) {
                  if (pair[0].ok) { applySlotState(card, pair[1]); cardError(card, ""); }
                  else { removeBtn.disabled = false; cardError(card, pair[1].error || t("remove_failed", "That file could not be removed.")); }
              })
              .catch(function () { removeBtn.disabled = false; cardError(card, t("remove_offline", "That file could not be removed. Check your connection.")); });
        } else {
            openPreview(partUrl(previewTemplate, id), card.getAttribute("data-title"),
                        li.querySelector("[data-part-name]").textContent);
        }
    });

    /* ── preview ─────────────────────────────────────────────── */
    var modalEl = document.querySelector("[data-part-preview]");
    var previewUrl = null;
    function openPreview(url, title, filename) {
        if (!modalEl || !window.bootstrap) { window.open(url, "_blank", "noopener"); return; }
        var body = modalEl.querySelector("[data-preview-body]");
        modalEl.querySelector("[data-preview-title]").textContent = title || "Your file";
        modalEl.querySelector("[data-preview-meta]").textContent = filename || "";
        body.textContent = t("loading", "Loading…");
        window.bootstrap.Modal.getOrCreateInstance(modalEl).show();
        fetch(url, { credentials: "same-origin" }).then(function (res) {
            if (!res.ok) throw new Error();
            return res.blob();
        }).then(function (blob) {
            if (previewUrl) URL.revokeObjectURL(previewUrl);
            previewUrl = URL.createObjectURL(blob);
            body.textContent = "";
            if (blob.type === "application/pdf") {
                var frame = document.createElement("iframe");
                frame.src = previewUrl;
                frame.title = t("preview_of", "Preview of {name}", { name: filename || t("your_file", "your file") });
                frame.className = "rg-intake-preview__frame";
                body.appendChild(frame);
                var hint = document.createElement("p");
                hint.className = "rg-intake-preview__fallback";
                hint.textContent = t("preview_blank", "If the preview is blank, your device can't show PDFs here. The file is still saved.");
                body.appendChild(hint);
            } else {
                var img = document.createElement("img");
                img.src = previewUrl;
                img.alt = t("preview_of", "Preview of {name}", { name: filename || t("your_file", "your file") });
                img.className = "rg-intake-preview__image";
                body.appendChild(img);
            }
        }).catch(function () { body.textContent = t("preview_failed", "This file couldn't be shown. It is still saved."); });
    }
    if (modalEl) {
        modalEl.addEventListener("hidden.bs.modal", function () {
            if (previewUrl) { URL.revokeObjectURL(previewUrl); previewUrl = null; }
            modalEl.querySelector("[data-preview-body]").textContent = "";
        });
    }

    /* ── progress + still-needed list ────────────────────────── */
    function updateProgress() {
        var cards = Array.prototype.filter.call(root.querySelectorAll("[data-doc-card]"), function (c) {
            return !c.hidden && c.getAttribute("data-required") === "true";
        });
        var ready = cards.filter(function (c) { return c.classList.contains("is-ready"); });
        var readyEl = root.querySelector("[data-doc-ready]");
        var totalEl = root.querySelector("[data-doc-total]");
        if (readyEl) readyEl.textContent = ready.length;
        if (totalEl) totalEl.textContent = cards.length;
        var stickyReady = root.querySelector("[data-doc-ready-sticky]");
        var stickyTotal = root.querySelector("[data-doc-total-sticky]");
        if (stickyReady) stickyReady.textContent = ready.length;
        if (stickyTotal) stickyTotal.textContent = cards.length;
        var sticky = root.querySelector("[data-doc-sticky]");
        if (sticky) sticky.classList.toggle("is-complete", ready.length === cards.length);
        var missingBox = root.querySelector("[data-doc-missing]");
        var list = root.querySelector("[data-doc-missing-list]");
        if (missingBox && list) {
            list.textContent = "";
            cards.forEach(function (c) {
                if (c.classList.contains("is-ready")) return;
                var li = document.createElement("li");
                var a = document.createElement("a");
                a.href = "#" + c.id;
                a.textContent = c.getAttribute("data-title");
                li.appendChild(a);
                list.appendChild(li);
            });
            missingBox.hidden = ready.length === cards.length;
        }
    }

    /* ── performance rating question (Plantilla) ─────────────── */
    var ratingCard = root.querySelector("[data-conditional-performance-rating]");
    root.querySelectorAll("input[name=performance_rating_applicability]").forEach(function (radio) {
        radio.addEventListener("change", function () {
            if (!ratingCard || !radio.checked) return;
            var applicable = radio.value === "applicable";
            if (!applicable && ratingCard.querySelector(".rg-doc-part")) {
                if (!window.confirm(t("confirm_drop_rating", "Remove the performance rating files you added?"))) {
                    var other = root.querySelector("input[name=performance_rating_applicability][value=applicable]");
                    if (other) other.checked = true;
                    return;
                }
                ratingCard.querySelectorAll(".rg-doc-part").forEach(function (li) { li.remove(); });
                ratingCard.classList.remove("is-ready");
            }
            ratingCard.hidden = !applicable;
            ratingCard.setAttribute("data-required", applicable ? "true" : "false");
            updateProgress();
        });
    });

    /* ── autosave typed answers ──────────────────────────────── */
    var statusEl = root.querySelector("[data-autosave-status]");
    var autosaveUrl = root.getAttribute("data-autosave-url");
    var timer = null;
    function autosave() {
        var data = new FormData(form);
        data.set("action", "autosave");
        data.delete("submission_confirmation");
        if (statusEl) statusEl.textContent = t("saving", "Saving…");
        fetch(autosaveUrl, {
            method: "POST",
            body: data,
            credentials: "same-origin",
            redirect: "manual",
            headers: { "X-Requested-With": "XMLHttpRequest" }
        }).then(function (res) {
            if (res.type === "opaqueredirect" || res.status === 403) {
                if (statusEl) statusEl.textContent = t("session_ended", "Your session ended. Copy anything you typed, then reload the page to continue.");
                return null;
            }
            return res.json();
        }).then(function (body) {
            if (!body || !statusEl) return;
            statusEl.textContent = body.ok ? t("saved", "Saved") : t("needs_attention", "Some answers need attention before they can be saved.");
        }).catch(function () { if (statusEl) statusEl.textContent = t("save_failed", "Couldn't save just now. We'll try again."); });
    }
    form.querySelectorAll("input[type=text], input[type=tel], textarea, input[type=radio]").forEach(function (el) {
        el.addEventListener(el.type === "radio" ? "change" : "input", function () {
            if (timer) clearTimeout(timer);
            timer = setTimeout(autosave, 1200);
        });
    });

    /* ── character counters ──────────────────────────────────── */
    root.querySelectorAll("[data-char-count-for]").forEach(function (counter) {
        var field = document.getElementById(counter.getAttribute("data-char-count-for"));
        if (!field) return;
        var limit = parseInt(field.getAttribute("data-char-limit") || field.getAttribute("maxlength"), 10);
        if (!limit) return;
        function refresh() {
            var used = field.value.length;
            counter.textContent = t("characters", "{used} / {limit} characters", { used: used, limit: limit });
            counter.classList.toggle("is-over", used > limit);
        }
        field.addEventListener("input", refresh);
        refresh();
    });

    /* ── submit ──────────────────────────────────────────────── */
    var submitBtn = root.querySelector("[data-submit-application]");
    form.addEventListener("submit", function (e) {
        var submitter = e.submitter;
        if (!submitter || submitter.value !== "submit") return;
        if (root.querySelector(".rg-doc-part.is-uploading")) {
            e.preventDefault();
            window.alert(t("wait_uploads", "Please wait until your files finish uploading."));
            return;
        }
        if (timer) clearTimeout(timer);
        submitBtn.disabled = true;
        submitBtn.textContent = t("submitting", "Submitting…");
        // Keep the clicked button's value: a disabled submitter is not sent.
        var hidden = document.createElement("input");
        hidden.type = "hidden";
        hidden.name = "action";
        hidden.value = "submit";
        form.appendChild(hidden);
    });

    var confirmBox = form.querySelector("input[name=submission_confirmation]");
    if (confirmBox) {
        confirmBox.addEventListener("change", function () {
            var err = confirmBox.closest(".rg-apply-confirm").querySelector(".invalid-feedback");
            if (err && confirmBox.checked) err.remove();
        });
    }

    var summary = document.querySelector("[data-error-summary]");
    if (summary) { try { summary.focus(); } catch (x) {} }
    updateProgress();
})();
