const form = document.querySelector("#chat-form");
const input = document.querySelector("#message");
const messages = document.querySelector("#messages");
const status = document.querySelector("#status");

function displayMessage(sender, text) {
  const paragraph = document.createElement("p");
  // Treat messages as text, never executable HTML.
  paragraph.textContent = `${sender}: ${text}`;
  messages.appendChild(paragraph);
}

const button = form.querySelector("button");
const protocol = location.protocol === "https:" ? "wss:" : "ws:";
const socket = new WebSocket(`${protocol}//${location.host}/ws/chat`);
button.disabled = true;
socket.onopen = () => {
  status.textContent = "Connected. Each message is independent.";
  button.disabled = false;
};
socket.onmessage = (event) => {
  const response = JSON.parse(event.data);
  displayMessage(response.error ? "Error" : "Gemini", response.error || response.reply);
  status.textContent = "Ready. No conversation history is sent.";
  button.disabled = false;
};
socket.onclose = () => {
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
