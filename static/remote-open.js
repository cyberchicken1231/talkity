// Small client helper: click once to "Allow remote open" (opens a blank window).
// Later incoming WS messages { type: "open", url: "...", by: "..." } will navigate that window.
(function () {
	// avoid double-including
	if (window.__remote_open_installed) return;
	window.__remote_open_installed = true;

	// UI: floating allow button
	const btn = document.createElement("button");
	btn.textContent = "Allow remote open";
	btn.style.position = "fixed";
	btn.style.bottom = "12px";
	btn.style.right = "12px";
	btn.style.zIndex = 99999;
	btn.style.padding = "8px 10px";
	btn.style.background = "#007bff";
	btn.style.color = "white";
	btn.style.border = "none";
	btn.style.borderRadius = "6px";
	btn.style.cursor = "pointer";
	btn.style.boxShadow = "0 2px 6px rgba(0,0,0,0.2)";
	btn.title = "Click once to allow the server to open links for you in a new tab";
	document.body.appendChild(btn);

	// store reference to the window that will later be navigated
	window.__remoteOpenWindow = null;

	function enableRemoteOpen() {
		try {
			// open a blank window/tab (user gesture) and keep a ref
			const w = window.open("", "_blank");
			if (w) {
				window.__remoteOpenWindow = w;
				btn.textContent = "Remote open allowed";
				btn.style.background = "#28a745";
				btn.disabled = true;
				btn.title = "Remote open enabled";
			} else {
				// fallback if popup blocked: notify user
				alert("Popup blocked. Please allow popups for this site and try again.");
			}
		} catch (e) {
			console.error("remote-open: open failed", e);
		}
	}

	btn.addEventListener("click", enableRemoteOpen, { once: true });

	// helper to handle open messages
	function handleOpenMessage(url) {
		try {
			// prefer navigating the pre-opened window (bypasses popup blockers)
			if (window.__remoteOpenWindow && !window.__remoteOpenWindow.closed) {
				window.__remoteOpenWindow.location.href = url;
				return true;
			}
			// otherwise attempt to open directly (may be blocked)
			const w = window.open(url, "_blank");
			if (w) return true;
			// last resort: ask the user
			if (confirm("Open URL requested by server:\n" + url + "\n\nOpen now?")) {
				window.open(url, "_blank");
				return true;
			}
		} catch (e) {
			console.error("remote-open: navigation failed", e);
		}
		return false;
	}

	// patch addEventListener for 'message' so existing code receives other messages unchanged
	const proto = WebSocket.prototype;
	const origAddEvent = proto.addEventListener;
	proto.addEventListener = function (type, listener, ...rest) {
		if (type === "message" && typeof listener === "function") {
			const wrapped = function (ev) {
				try {
					const d = JSON.parse(ev.data);
					if (d && d.type === "open" && d.url) {
						// handle and do not swallow other handlers: still call original listener
						handleOpenMessage(d.url);
					}
				} catch (err) {
					// not JSON or no-op
				}
				listener.call(this, ev);
			};
			return origAddEvent.call(this, type, wrapped, ...rest);
		}
		return origAddEvent.call(this, type, listener, ...rest);
	};

	// patch onmessage setter/getter so ws.onmessage = fn still works
	const desc = Object.getOwnPropertyDescriptor(proto, "onmessage") || {};
	if (desc && desc.configurable !== false) {
		const originalSetter = desc.set;
		const originalGetter = desc.get;
		Object.defineProperty(proto, "onmessage", {
			get: function () {
				return originalGetter ? originalGetter.call(this) : this.__onmessage;
			},
			set: function (fn) {
				if (typeof fn !== "function") {
					if (originalSetter) originalSetter.call(this, fn);
					else this.__onmessage = fn;
					return;
				}
				const wrapped = function (ev) {
					try {
						const d = JSON.parse(ev.data);
						if (d && d.type === "open" && d.url) {
							handleOpenMessage(d.url);
						}
					} catch (err) {}
					return fn.call(this, ev);
				};
				if (originalSetter) originalSetter.call(this, wrapped);
				else this.__onmessage = wrapped;
			},
			configurable: true,
			enumerable: true,
		});
	}

	// expose helper on window for debugging
	window.remoteOpen = {
		enable: enableRemoteOpen,
		handleOpenMessage,
	};
})();
