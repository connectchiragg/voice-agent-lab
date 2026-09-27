const form = document.querySelector("#chat-form");
const input = document.querySelector("#message");
const messages = document.querySelector("#messages");
const status = document.querySelector("#status");

function displayMessage(sender, text) {
  const paragraph = document.createElement("p");
  // Treat messages as text, never executable HTML.
  paragraph.textContent = `${sender}: ${text}`;
  messages.appendChild(paragraph);
  return paragraph;
}

let assistantMessage = null;

const button = form.querySelector("button");
const protocol = location.protocol === "https:" ? "wss:" : "ws:";
let conversationId = sessionStorage.getItem("conversationId");
if (!conversationId) {
  conversationId = crypto.randomUUID();
  sessionStorage.setItem("conversationId", conversationId);
}
const socket = new WebSocket(`${protocol}//${location.host}/ws/chat?conversation_id=${encodeURIComponent(conversationId)}`);
document.querySelector("#new-conversation").addEventListener("click", () => {
  sessionStorage.setItem("conversationId", crypto.randomUUID());
  location.reload();
});
button.disabled = true;
socket.onopen = () => {
  status.textContent = "Connected.";
  button.disabled = false;
};
socket.onmessage = (event) => {
  const response = JSON.parse(event.data);
  if (response.history) {
    messages.replaceChildren();
    for (const message of response.history) {
      displayMessage(message.role === "user" ? "You" : "Gemini",
        message.parts[0].text + (message.status === "interrupted" ? " [Interrupted]" : ""));
    }
    return;
  }
  if (response.type === "start") {
    assistantMessage = displayMessage("Gemini", "");
    return;
  }
  if (response.type === "delta") {
    assistantMessage.appendChild(document.createTextNode(response.text));
    return;
  }
  if (response.type === "interrupted" && assistantMessage) {
    assistantMessage.appendChild(document.createTextNode(" [Interrupted]"));
  }
  if (response.error) displayMessage("Error", response.error);
  assistantMessage = null;
  status.textContent = "Ready.";
  button.disabled = false;
};
socket.onclose = () => {
  if (assistantMessage) assistantMessage.appendChild(document.createTextNode(" [Connection lost]"));
  status.textContent = "Disconnected. Reload to reconnect.";
  button.disabled = true;
};
socket.onerror = () => { status.textContent = "Connection error."; };

form.addEventListener("submit", (event) => {
  event.preventDefault();
  const message = input.value.trim();
  if (!message || socket.readyState !== WebSocket.OPEN || button.disabled) return;
  displayMessage("You", message);
  socket.send(message);
  input.value = "";
  button.disabled = true;
  status.textContent = "Waiting for Gemini…";
});
