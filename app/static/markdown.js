(function (global) {
  function createMarkdownRenderer(markdown, purifier) {
    if (!markdown || !purifier) {
      throw new Error('Markdown renderer dependencies are unavailable');
    }

    purifier.addHook('afterSanitizeAttributes', (node) => {
      if (node.tagName === 'A' && node.hasAttribute('href')) {
        node.setAttribute('target', '_blank');
        node.setAttribute('rel', 'noopener noreferrer');
      }
    });

    return function renderMarkdown(text) {
      const html = markdown.parse(String(text ?? ''), { gfm: true, breaks: false });
      const sanitized = purifier.sanitize(html, { USE_PROFILES: { html: true } });
      const template = document.createElement('template');
      template.innerHTML = sanitized;

      for (const table of template.content.querySelectorAll('table')) {
        const wrapper = document.createElement('div');
        wrapper.className = 'markdown-table-wrap';
        table.replaceWith(wrapper);
        wrapper.append(table);
      }

      return template.innerHTML;
    };
  }

  global.createMarkdownRenderer = createMarkdownRenderer;
  global.renderMarkdown = createMarkdownRenderer(global.marked, global.DOMPurify);
})(window);
