const form = document.querySelector("#add-task");
const input = document.querySelector("#task-title");
const submit = form.querySelector("button");
const feedback = document.querySelector("#feedback");
const count = document.querySelector("#saved-count");
const empty = document.querySelector("#empty");
const list = document.querySelector("#task-list");
const endpoint = "/api/task" + window.location.search;

function renderTask(title) {
  const item = document.createElement("li");
  item.textContent = title;
  list.replaceChildren(...(title ? [item] : []));
  list.hidden = !title;
  empty.textContent = title ? "" : "还没有任务。";
  empty.hidden = Boolean(title);
  count.textContent = `已保存任务：${title ? 1 : 0}`;
}

async function requestTask(options) {
  const response = await fetch(endpoint, options);
  if (!response.ok) throw new Error(String(response.status));
  return response.json();
}

async function loadTask() {
  try {
    const { title, variant } = await requestTask();
    submit.textContent = variant === "renamed" ? "保存任务" : "添加任务";
    renderTask(title);
    input.disabled = submit.disabled = false;
  } catch {
    count.textContent = "无法读取任务";
    feedback.textContent = "请刷新页面重试。";
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const title = input.value.trim();
  if (!title) return;
  submit.disabled = true;
  feedback.textContent = "";
  try {
    const task = await requestTask({
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title }),
    });
    renderTask(task.title);
    feedback.textContent = "已添加任务";
    input.value = "";
  } catch {
    feedback.textContent = "保存请求失败，请重试。";
  } finally {
    submit.disabled = false;
  }
});

loadTask();
