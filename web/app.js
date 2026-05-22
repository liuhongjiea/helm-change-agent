// ─── Main page app ────────────────────────────────────────────────────────────

function app() {
  return {
    // State
    loading: true,
    error: null,
    view: 'form',  // 'form' | 'progress' | 'history'
    options: { clusters: [], components: [], cluster_to_components: {}, component_specs: {} },
    form: {
      cluster: '',
      component: '',
      source: 'miks-proxy',
      kcFile: null,
      kcFileName: '',
    },
    analyzing: false,
    steps: [
      { id: 'render', label: '渲染仓库配置 (helm template)', status: 'pending', meta: '' },
      { id: 'fetch',  label: '拉取集群现状 (kubectl get)',  status: 'pending', meta: '' },
      { id: 'diff',   label: '配置对比',                   status: 'pending', meta: '' },
      { id: 'ai',     label: 'AI 风险评估',                status: 'pending', meta: '' },
    ],
    progressError: null,
    history: [],
    historyLoading: false,

    async init() {
      try {
        const res = await fetch('/api/options');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        this.options = await res.json();
        this.loading = false;
      } catch (e) {
        this.error = `无法加载组件索引: ${e.message}`;
        this.loading = false;
      }
    },

    get filteredComponents() {
      if (!this.form.cluster) return this.options.components || [];
      return (this.options.cluster_to_components || {})[this.form.cluster] || [];
    },

    get componentMeta() {
      if (!this.form.component) return null;
      const specs = (this.options.component_specs || {})[this.form.component];
      if (!specs || !specs.length) return null;
      const s = specs[0];
      const chartDir = s.chart_dir || '';
      // Show only last 3 path segments for brevity
      const parts = chartDir.split('/');
      const chartDirShort = parts.slice(-3).join('/');
      return { ...s, specs, chart_dir_short: chartDirShort };
    },

    get canSubmit() {
      if (!this.form.cluster || !this.form.component) return false;
      if (this.form.source === 'kubeconfig-upload' && !this.form.kcFile) return false;
      return !this.analyzing;
    },

    onClusterChange() {
      this.form.component = '';
    },

    onComponentChange() {},

    onKcFile(event) {
      const file = event.target.files[0];
      if (file) {
        this.form.kcFile = file;
        this.form.kcFileName = file.name;
      }
    },

    resetSteps() {
      this.steps = this.steps.map(s => ({ ...s, status: 'pending', meta: '' }));
      this.progressError = null;
    },

    setStep(id, status, meta = '') {
      const s = this.steps.find(s => s.id === id);
      if (s) { s.status = status; s.meta = meta; }
    },

    async startAnalyze() {
      this.analyzing = true;
      this.resetSteps();
      this.view = 'progress';

      const formData = new FormData();
      formData.append('cluster', this.form.cluster);
      formData.append('component', this.form.component);
      formData.append('source_type', this.form.source);
      if (this.form.kcFile) {
        formData.append('kubeconfig_file', this.form.kcFile);
      }

      try {
        const res = await fetch('/api/analyze', { method: 'POST', body: formData });
        if (!res.ok) {
          throw new Error(`HTTP ${res.status}: ${await res.text()}`);
        }

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });

          const lines = buffer.split('\n');
          buffer = lines.pop();  // keep incomplete last line

          for (const line of lines) {
            if (!line.startsWith('data: ')) continue;
            try {
              const event = JSON.parse(line.slice(6));
              this._handleSSEEvent(event);
            } catch {}
          }
        }
      } catch (e) {
        this.progressError = e.message;
      } finally {
        this.analyzing = false;
      }
    },

    _handleSSEEvent(ev) {
      const { step, status, msg, ms, resource_count, added, removed, changed, risk, report_id, fallback_hint } = ev;

      if (step === 'render') {
        if (status === 'running') this.setStep('render', 'running');
        else if (status === 'done') this.setStep('render', 'done', `${resource_count} 个资源 · ${ms}ms`);
        else if (status === 'error') { this.setStep('render', 'error', msg); this.progressError = msg; }

      } else if (step === 'fetch') {
        if (status === 'running') this.setStep('fetch', 'running', `via ${ev.via || ''}`);
        else if (status === 'done') this.setStep('fetch', 'done', `${ms}ms`);
        else if (status === 'error') {
          this.setStep('fetch', 'error', msg);
          this.progressError = msg + (fallback_hint ? ` — ${fallback_hint}` : '');
        }

      } else if (step === 'diff') {
        if (status === 'running') this.setStep('diff', 'running');
        else if (status === 'done') {
          const meta = `+${added} 新增 · -${removed} 删除 · ~${changed} 变更`;
          this.setStep('diff', 'done', meta);
        }

      } else if (step === 'ai') {
        if (status === 'running') this.setStep('ai', 'running');
        else if (status === 'done') {
          const riskLabel = { high: '🔴 高风险', medium: '🟡 中风险', low: '🟢 低风险' }[risk] || '⚪ 分析完成';
          this.setStep('ai', 'done', `${riskLabel} · ${ms}ms`);
        } else if (status === 'error') { this.setStep('ai', 'error', msg); }

      } else if (step === 'done') {
        // Auto-navigate to report
        setTimeout(() => {
          window.location.href = `/report.html?id=${report_id}`;
        }, 800);
      }
    },

    async loadHistory() {
      this.historyLoading = true;
      try {
        const res = await fetch('/api/reports');
        this.history = await res.json();
      } catch {}
      this.historyLoading = false;
    },

    formatTime(ts) {
      const d = new Date(ts * 1000);
      return d.toLocaleDateString('zh-CN') + ' ' + d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
    },

    resetForm() {
      this.view = 'form';
      this.analyzing = false;
      this.progressError = null;
    },
  };
}


// ─── Report page app ──────────────────────────────────────────────────────────

function reportApp() {
  return {
    loading: true,
    error: null,
    rawMd: '',
    renderedHtml: '',
    meta: { component: '', cluster: '', namespace: '', generated_at: '', risk: '' },
    copied: false,
    sections: [
      { id: 'section-1', label: '一、AI 智能变更分析' },
      { id: 'section-2', label: '二、配置变更详情' },
      { id: 'section-3', label: '三、资源清单变更' },
      { id: 'section-4', label: '四、操作检查清单' },
      { id: 'section-5', label: '五、备注' },
    ],

    get riskClass() {
      const r = this.meta.risk;
      if (!r) return 'unknown';
      if (r.includes('高') || r.includes('high')) return 'high';
      if (r.includes('中') || r.includes('medium')) return 'medium';
      if (r.includes('低') || r.includes('low')) return 'low';
      return 'unknown';
    },

    get riskLabel() {
      return this.meta.risk || '加载中...';
    },

    async init() {
      const params = new URLSearchParams(window.location.search);
      const id = params.get('id');
      if (!id) {
        this.error = '缺少报告 ID (url 参数 ?id=...)';
        this.loading = false;
        return;
      }

      try {
        const res = await fetch(`/api/reports/${id}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        if (data.error) throw new Error(data.error);
        this.rawMd = data.content;
        this._parseMeta(data.content);
        this._render(data.content);
        this.loading = false;
      } catch (e) {
        this.error = `无法加载报告: ${e.message}`;
        this.loading = false;
      }
    },

    _parseMeta(md) {
      const lines = md.split('\n');
      for (const line of lines) {
        if (line.startsWith('**分析组件**:')) this.meta.component = line.split(':')[1]?.trim();
        if (line.startsWith('**目标集群**:')) this.meta.cluster = line.split(':')[1]?.trim();
        if (line.startsWith('**命名空间**:')) this.meta.namespace = line.split(':')[1]?.trim();
        if (line.startsWith('**生成时间**:')) this.meta.generated_at = line.split(':').slice(1).join(':').trim();
        if (line.startsWith('**风险等级**:')) this.meta.risk = line.split(':')[1]?.trim();
      }
    },

    _render(md) {
      if (typeof marked === 'undefined') {
        this.renderedHtml = `<pre>${md.replace(/</g, '&lt;')}</pre>`;
        return;
      }

      // Render markdown to HTML
      const html = marked.parse(md, { breaks: true, gfm: true });

      // Post-process in a detached DOM node
      const tmp = document.createElement('div');
      tmp.innerHTML = html;

      // Add section IDs to h2 headings (for left-nav anchors)
      let h2Idx = 0;
      tmp.querySelectorAll('h2').forEach(h => {
        h2Idx++;
        h.id = `section-${h2Idx}`;
      });

      // Syntax-highlight code blocks
      if (typeof hljs !== 'undefined') {
        tmp.querySelectorAll('pre code').forEach(block => {
          hljs.highlightElement(block);
        });
      }

      this.renderedHtml = tmp.innerHTML;
    },

    downloadMd() {
      const blob = new Blob([this.rawMd], { type: 'text/markdown' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      const params = new URLSearchParams(window.location.search);
      a.href = url;
      a.download = (params.get('id') || 'report') + '.md';
      a.click();
      URL.revokeObjectURL(url);
    },

    async copyMd() {
      try {
        await navigator.clipboard.writeText(this.rawMd);
        this.copied = true;
        setTimeout(() => { this.copied = false; }, 2000);
      } catch {}
    },
  };
}
