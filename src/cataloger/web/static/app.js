let currentSessionId = null;

async function generate() {
  const locationInput = document.getElementById("location");
  const isbnsInput = document.getElementById("isbns");
  const location = locationInput.value.trim();
  const raw = isbnsInput.value.trim();

  if (!location) {
    locationInput.focus();
    return;
  }
  if (!raw) {
    isbnsInput.focus();
    return;
  }

  const isbns = raw
    .split("\n")
    .map((s) => s.trim())
    .filter(Boolean);

  if (isbns.length === 0) return;

  // Show progress
  show("progress-section");
  hide("input-section");
  hide("results-section");

  const progressFill = document.getElementById("progress-fill");
  const progressText = document.getElementById("progress-text");
  progressFill.value = 20;
  progressText.textContent = `Looking up ${isbns.length} book${isbns.length > 1 ? "s" : ""}...`;

  document.getElementById("generate-btn").disabled = true;

  try {
    const resp = await fetch("/api/lookup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ isbns, location }),
    });

    progressFill.value = 90;

    if (!resp.ok) {
      const err = await resp.json();
      throw new Error(err.error || "Request failed");
    }

    const data = await resp.json();
    currentSessionId = data.session_id;

    progressFill.value = 100;
    progressText.textContent = "Done!";

    setTimeout(() => showResults(data), 300);
  } catch (err) {
    hide("progress-section");
    show("input-section");
    showError(err.message);
    document.getElementById("generate-btn").disabled = false;
  }
}

function showResults(data) {
  hide("progress-section");
  show("results-section");

  const { summary, books } = data;

  document.getElementById("summary").innerHTML =
    `<strong>${summary.found}</strong> of ${summary.total} books found · ` +
    `<strong>${summary.images}</strong> cover images · ` +
    `${summary.missing} not found`;

  const tbody = document.getElementById("results-body");
  tbody.innerHTML = "";

  for (const book of books) {
    const tr = document.createElement("tr");

    // Cover
    const coverTd = document.createElement("td");
    if (book.image_url) {
      const img = document.createElement("img");
      img.src = book.image_url;
      img.alt = book.title || book.isbn;
      img.className = "w-10 h-14 object-cover rounded";
      img.loading = "lazy";
      coverTd.appendChild(img);
    } else {
      const div = document.createElement("div");
      div.className = "w-10 h-14 bg-base-200 rounded flex items-center justify-center text-xs opacity-50";
      div.textContent = "N/A";
      coverTd.appendChild(div);
    }
    tr.appendChild(coverTd);

    // ISBN
    const isbnTd = document.createElement("td");
    isbnTd.textContent = book.isbn;
    tr.appendChild(isbnTd);

    // Title
    const titleTd = document.createElement("td");
    titleTd.textContent = book.title || "—";
    tr.appendChild(titleTd);

    // Author
    const authorTd = document.createElement("td");
    authorTd.textContent = book.author || "—";
    tr.appendChild(authorTd);

    // Price
    const priceTd = document.createElement("td");
    priceTd.textContent = book.price ? `$${book.price}` : "—";
    tr.appendChild(priceTd);

    // Status
    const statusTd = document.createElement("td");
    const statusBadge = document.createElement("span");
    if (book.errors.length > 0) {
      statusBadge.className = "badge badge-error";
      statusBadge.textContent = book.errors.join(", ");
    } else {
      statusBadge.className = "badge badge-success";
      statusBadge.textContent = "OK";
    }
    statusTd.appendChild(statusBadge);
    tr.appendChild(statusTd);

    tbody.appendChild(tr);
  }
}

function downloadFile(type) {
  if (!currentSessionId) return;
  const url = `/api/download/${type}?session=${currentSessionId}`;
  window.location.href = url;
}

function reset() {
  currentSessionId = null;
  hide("results-section");
  hide("progress-section");
  show("input-section");
  document.getElementById("generate-btn").disabled = false;
  document.getElementById("progress-fill").value = 0;
}

function showError(msg) {
  // Remove any existing error
  const existing = document.querySelector(".alert-error");
  if (existing) existing.remove();

  const div = document.createElement("div");
  div.className = "alert alert-error mb-4";
  div.textContent = msg;
  document.getElementById("input-section").prepend(div);

  setTimeout(() => div.remove(), 5000);
}

function show(id) {
  document.getElementById(id).classList.remove("hidden");
}

function hide(id) {
  document.getElementById(id).classList.add("hidden");
}
