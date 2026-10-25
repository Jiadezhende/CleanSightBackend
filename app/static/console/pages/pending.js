// 尚未迁到控制台的页面：给出旧页面入口。整批迁完后连同本文件删除。
export function pendingPage(title, oldUrl) {
  return {
    template: `
<div class="page">
  <div class="card card-b">
    <h3 style="margin:0 0 6px">{{ title }}</h3>
    <div class="sub">这一页还在迁移中，暂时请用旧页面：<a :href="oldUrl" target="_blank">{{ oldUrl }}</a></div>
  </div>
</div>`,
    setup: () => ({ title, oldUrl }),
  };
}
