/* Public static UI. No API secrets, author-code execution, or HTML interpolation. */
(function () {
  'use strict';
  const STATUS = {seed:'人工种子 · 待自动核查',pending:'候选 / 待核',ready:'静态核查就绪',partial:'部分完成',error:'本轮异常',excluded:'已排除 · 检查未通过'};
  const safeURL = value => {
    if (typeof value !== 'string') return '';
    try {
      const url = new URL(value);
      if (url.protocol !== 'https:' || url.username || url.password || !url.hostname || url.port) return '';
      if ([...url.searchParams.keys()].some(key => /^(?:api[_-]?key|key|token|secret|access_token|authorization|signature|credential)$/i.test(key))) return '';
      return url.href;
    } catch (_) { return ''; }
  };
  const safeAsset = value => typeof value === 'string' && /^assets\/[A-Za-z0-9][A-Za-z0-9._/-]*$/.test(value) && !value.includes('//') && value.split('/').every(part => part !== '..' && part !== '.') ? value : '';
  const str = (value, fallback = '待核') => value === undefined || value === null || value === '' ? fallback : Array.isArray(value) ? value.map(item => str(item, '')).filter(Boolean).join(' · ') : typeof value === 'object' ? fallback : String(value);
  const list = value => Array.isArray(value) ? value : [];
  const STAGE_LABELS = {metadata:'正式文献核查',article:'全文与原图',repository:'作者代码静态检查',metrics:'期刊指标',ai:'AI 中文注释',citations:'引用指标',discovery:'新文献发现',repair:'既有案例修补',candidate:'候选核查',summary:'AI 中文注释'};
  const STAGE_STATUS_LABELS = {...STATUS,unknown:'状态待核',ok:'完成',success:'完成',generated:'已生成',complete:'完成',completed:'完成',matched:'已匹配',inspected:'已静态检查',stale:'保留旧结果 · 待更新',deferred:'等待后续处理',deferred_until_selection:'等待入选后生成',disabled:'本轮未启用',not_checked:'本轮未检查',not_found:'未找到匹配结果',not_applicable:'不适用',unavailable:'本轮来源不可用',metadata_unavailable:'正式文献来源不可用',missing_key:'尚未配置 API 密钥',missing_model:'尚未配置模型',missing_evidence:'已核来源证据不足',missing_endpoint:'尚未配置接口地址',invalid_endpoint:'接口地址未通过校验',invalid_response:'接口响应未通过校验',budget_exhausted:'达到本轮本地请求上限',authentication_error:'身份认证失败（HTTP 401）',payment_required:'服务方要求付款或账户额度不足（HTTP 402）',permission_denied:'接口拒绝访问（HTTP 403）',invalid_request:'请求参数被接口拒绝（HTTP 400）',endpoint_not_found:'接口路径不存在（HTTP 404）',rate_limited:'服务方限流（HTTP 429）',service_unavailable:'服务方暂时不可用（HTTP 5xx）',other_http_error:'其它 HTTP 请求失败',network_error:'网络请求失败',invalid_doi:'DOI 无效',doi_mismatch:'DOI 与来源不一致',retracted:'已撤稿',no_final_version:'尚未核实正式版本',no_scripts_found:'未找到可检查脚本',partial_failure:'本轮部分失败'};
  const stageLabel = name => typeof name === 'string' && Object.hasOwn(STAGE_LABELS,name) ? STAGE_LABELS[name] : '未分类阶段';
  const statusLabel = value => typeof value === 'string' && Object.hasOwn(STAGE_STATUS_LABELS,value) ? STAGE_STATUS_LABELS[value] : '状态待核';
  const validHTTP = value => Number.isInteger(value) && value >= 400 && value <= 599;
  const httpStatusLabel = value => validHTTP(value) ? statusLabel(({400:'invalid_request',401:'authentication_error',402:'payment_required',403:'permission_denied',404:'endpoint_not_found',429:'rate_limited'})[value] || (value >= 500 ? 'service_unavailable' : 'other_http_error')) + ([400,401,402,403,404,429].includes(value) ? '' : ' · HTTP '+value) : '';
  const reasonLabel = value => {
    const budgets={network_budget_exhausted:'本轮本地总 HTTP 请求上限已到',collection_budget_exhausted:'本轮本地采集请求上限已到',ai_budget_exhausted:'本轮本地 AI 尝试上限已到'};
    return typeof value === 'string' && Object.hasOwn(budgets,value) ? budgets[value] : typeof value === 'string' && /^http_[45]\d{2}$/.test(value) ? httpStatusLabel(Number(value.slice(5))) : ['error','network_error','invalid_endpoint','invalid_response','missing_key','missing_model','missing_evidence','missing_endpoint'].includes(value) ? statusLabel(value) : '';
  };
  const diagnosticLabel = value => {
    if (!value || typeof value !== 'object') return statusLabel(value);
    const status=value.last_attempt_status || value.status, label=statusLabel(status), reason=reasonLabel(value.error_reason) || httpStatusLabel(value.http_status);
    return reason && (status === 'budget_exhausted' || label === '状态待核' || reason.startsWith(label)) ? reason : reason && reason !== label ? label+' · '+reason : label;
  };
  const errorLabel = value => {
    if (value && typeof value === 'object') return value.error_reason || validHTTP(value.http_status) || value.status ? diagnosticLabel({...value,status:value.status || value.error}) : errorLabel(value.error);
    if (value === '正式版本/撤稿检查未通过') return value;
    if (typeof value !== 'string') return '本轮异常（原因未分类）';
    const match=value.match(/^([a-z_]+):\s*([a-z_]+)$/);
    if(match && Object.hasOwn(STAGE_LABELS,match[1]) && Object.hasOwn(STAGE_STATUS_LABELS,match[2]))return stageLabel(match[1])+'：'+statusLabel(match[2]);
    return Object.hasOwn(STAGE_STATUS_LABELS,value) ? statusLabel(value) : '本轮异常（原因未分类）';
  };
  const failureLabel = failure => {
    const doi=typeof failure?.doi === 'string' && /^10\.\d{4,9}\/[^\s<>]{1,200}$/i.test(failure.doi) ? failure.doi+' · ' : '';
    return doi+stageLabel(failure?.stage)+' · '+errorLabel(failure);
  };
  const counter = value => Number.isInteger(value) && value >= 0 && value <= 1000000 ? value : null;
  const updateBudgetLines = report => {
    const lines=[], requests=counter(report.requests), collection=counter(report.collectionRequests), ai=counter(report.aiAttempts), maximum=counter(report.maxRequests), maxAi=counter(report.maxAiCalls), reserved=counter(report.reservedAiRequests), repaired=counter(report.aiRepairCount);
    if(requests !== null || collection !== null || ai !== null)lines.push('总 HTTP 请求：'+(requests ?? '未报告')+(maximum === null ? '' : ' / '+maximum)+' 次 · 采集 GET：'+(collection ?? '未报告')+' 次 · AI HTTP 尝试：'+(ai ?? '未报告')+(maxAi === null ? '' : ' / '+maxAi)+' 次（失败重试也计数）');
    if(reserved !== null && reserved > 0)lines.push('本轮为 AI 预留 '+reserved+' 个总请求名额；采集上限'+(maximum === null ? '由预留名额限制' : '为 '+Math.max(0,maximum-reserved)+' 次')+'。预留不代表已经调用或扣费。');
    if(repaired !== null)lines.push('既有案例 AI 修补成功：'+repaired+' 条；本轮已匹配正式文献、已检查全文与作者代码的缺失、旧版或失败注释优先处理。');
    if(report.collectionBudgetExhausted === true)lines.push('采集达到本轮本地请求上限并停止扩展。'+(reserved > 0 ? '已为 AI 预留名额；实际使用情况见 AI HTTP 尝试计数。' : '后续采集等待下次更新。'));
    const aiBudgetSkipped=counter(report.stageCounts?.ai?.budget_exhausted)>0 || list(report.failures).some(failure=>['ai','summary'].includes(failure?.stage) && (failure?.status === 'budget_exhausted' || failure?.error === 'budget_exhausted'));
    if(ai === 0 && aiBudgetSkipped)lines.push('本轮 AI 因本地请求上限跳过，实际 AI HTTP 尝试为 0 次；未发起付费 AI 请求。');
    return lines;
  };
  const statusOf = paper => STATUS[paper.automation?.status] ? paper.automation.status : 'seed';
  const approvedTags = (paper, axis) => {
    const field = axis === 'forms' ? 'graphForms' : 'analysisFunctions';
    const statusField = axis === 'forms' ? 'formClassificationStatus' : 'functionClassificationStatus';
    if (!['approved','verified'].includes(paper[statusField])) return [];
    return [...new Set(list(paper[field]).filter(value => typeof value === 'string' && value))];
  };
  const matchAxis = (paper, axis, selected) => !selected || (selected === '__pending' ? !approvedTags(paper, axis).length : approvedTags(paper, axis).includes(selected));
  const safeFigures = paper => list(paper.article?.figures).filter(figure => figure.reuseStatus === 'allowed' && safeAsset(figure.asset));
  const figureReference = label => {
    const value=str(label,'');if(!value.trim())return null;
    let scope=/extended\s*(?:data|fig)|扩展/i.test(value)?'extended':/supplement|supporting|补充|附图/i.test(value)?'supplementary':'main';
    let match=value.match(/(?:\bfig(?:ure)?s?|图)\.?\s*(s?)(\d+)(?:\s*([a-z])(?=$|[^a-z]))?/i);
    if(!match&&scope==='extended')match=value.match(/extended\s*data\s*(s?)(\d+)(?:\s*([a-z])(?=$|[^a-z]))?/i);
    if(!match)return null;if(match[1].toLowerCase()==='s')scope='supplementary';
    return {scope,number:String(Number(match[2])),panel:(match[3]||'').toLowerCase()};
  };
  const figureNumber = label => {const reference=figureReference(label);return reference?Number(reference.number):null;};
  const cardFigure = paper => {
    const selected=figureReference(paper.figure), figures=list(paper.article?.figures);
    const matching=figure=>{const reference=figure.reference&&['main','extended','supplementary'].includes(figure.reference.scope)&&/^\d+$/.test(str(figure.reference.number,''))?figure.reference:figureReference(figure.label);return reference&&reference.scope===selected.scope&&String(Number(reference.number))===selected.number;};
    if(selected)return figures.find(figure=>matching(figure)&&figure.reuseStatus==='allowed'&&safeAsset(figure.asset)) || figures.find(matching) || null;
    if(str(paper.figure,'').trim())return null; // An unparsed selected label must never substitute another figure.
    return safeFigures(paper)[0] || null;
  };
  const inputSections = (spec, repositoryInputs = []) => ({
    selected:list(spec?.files).length ? spec.files : list(repositoryInputs),
    automatic:list(spec?.automaticInputs?.files),
  });
  const aiStateLabel = paper => paper.ai?.status === 'stale' ? paper.ai.last_attempt_status ? '旧注释 · 本轮重生成未完成' : '旧注释 · 来源已更新，待重生成' : paper.ai?.review_required || paper.ai?.reviewRequired ? 'AI 学习注释待核' : '';
  const viewFromHash = (hash,current='gallery') => ['gallery','papers','updates'].includes(String(hash).replace(/^#/,'')) ? String(hash).replace(/^#/,'') : current;
  const facetCounts = (papers, axis, otherSelected = '', predicate = () => true) => {
    const other = axis === 'forms' ? 'functions' : 'forms';
    const subset = list(papers).filter(paper => predicate(paper) && matchAxis(paper,other,otherSelected)), counts = new Map();
    let pending=0;
    for(const paper of subset){const tags=approvedTags(paper,axis);if(!tags.length)pending++;for(const id of tags)counts.set(id,(counts.get(id)||0)+1);}
    return {total:subset.length,pending,counts};
  };
  const citationCount = paper => {
    const value = paper.citations?.cited_by_count ?? paper.citationMetrics?.cited_by_count ?? paper.citation_count;
    return Number.isFinite(Number(value)) && value !== null && value !== '' && Number(value) >= 0 ? Number(value) : null;
  };
  const metricLabel = paper => {
    const metrics = paper.metrics || {};
    const parts = [];
    const retained=field=>metrics.retained_field_asof?.[field]?'〔沿用 '+str(metrics.retained_field_asof[field]).slice(0,10)+'〕':'';
    if (metrics.if !== undefined && metrics.if !== null && metrics.if !== '') parts.push('IF ' + str(metrics.if)+retained('if'));
    if (metrics.jcr) parts.push('JCR ' + str(metrics.jcr)+retained('jcr'));
    if (!parts.length) return '期刊指标待获取 / 未匹配';
    parts.push('指标年份：' + (['unknown','未知',''].includes(str(metrics.metric_year,'')) ? '接口未提供' : str(metrics.metric_year, '接口未提供')));
    return parts.join(' · ');
  };
  if (typeof module !== 'undefined' && module.exports) module.exports = {safeURL,safeAsset,approvedTags,matchAxis,safeFigures,citationCount,metricLabel,statusOf,inputSections,aiStateLabel,facetCounts,figureNumber,cardFigure,figureReference,viewFromHash,stageLabel,statusLabel,diagnosticLabel,errorLabel,failureLabel,updateBudgetLines};
  if (typeof document === 'undefined') return;

  let catalog = null, reports = [], view = 'gallery', activePaper = null, activeTab = 'inputs', previousFocus = null, previousImageFocus = null;
  const $ = id => document.getElementById(id);
  const state = {form:'',function:'',status:'',search:'',sort:'evidence'};
  const labels = {forms:new Map(),functions:new Map()};
  const el = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = String(text);
    if (className) node.className = className;
    return node;
  };
  function paragraph(parent, text, className) { const node = el('p', str(text, ''), className); parent.append(node); return node; }
  function heading(parent, text, level = 3) { const node = el('h' + level, text); parent.append(node); return node; }
  function link(text, value, className) {
    const url = safeURL(value);
    if (!url) return el('span', text + '（链接待核）', className);
    const node = el('a', text, className); node.href = url; node.target = '_blank'; node.rel = 'noopener noreferrer'; return node;
  }
  function items(parent, values, emptyText) {
    if (!list(values).length) { if (emptyText) paragraph(parent, emptyText); return; }
    const ul = el('ul');
    for (const value of values) ul.append(el('li', typeof value === 'object' ? str(value.message || value.reason || value.evidence || value.status) : str(value)));
    parent.append(ul);
  }
  function pill(paper) { const status = statusOf(paper); return el('span', STATUS[status], 'status-pill ' + status); }
  function tagGroup(paper) {
    const group = el('div', null, 'tag-group');
    for (const axis of ['forms','functions']) {
      const tags = approvedTags(paper, axis);
      if (!tags.length) group.append(el('span', axis === 'forms' ? '图形待核' : '功能待核', 'tag pending'));
      for (const id of tags) group.append(el('span', labels[axis].get(id) || id, 'tag' + (axis === 'functions' ? ' function' : '')));
    }
    return group;
  }
  function figurePlaceholder(paper, figure) {
    const placeholder = el('div', null, 'figure-placeholder');
    placeholder.append(el('span', str(figure?.label || paper.figure, '原图待定位'), 'figure-label'));
    placeholder.append(el('span', figure ? '图像许可或下载状态未满足展示条件' : '未附可再发布的官方原图', 'placeholder-caption'));
    placeholder.append(link('查看官方图 / 论文', figure?.sourceURL || paper.figureURL || paper.paperURL));
    return placeholder;
  }
  function figurePreview(paper, figure) {
    const node = el('div', null, 'figure-preview');
    const verified = figure && figure.reuseStatus === 'allowed' && safeAsset(figure.asset);
    if (verified) {
      node.classList.add('has-image');
      const image = el('img'); image.src = verified; image.alt = str(figure.label || paper.figure) + ' · 作者论文原图'; image.loading = 'lazy'; image.decoding = 'async';
      image.addEventListener('error', () => { image.remove(); node.classList.remove('has-image'); node.replaceChildren(figurePlaceholder(paper, figure)); }, {once:true});
      const zoom=el('button','放大原图 ↗','figure-zoom');zoom.type='button';zoom.addEventListener('click',()=>openImageViewer(paper,figure));
      const actions=el('div',null,'figure-actions');actions.append(el('span', str(figure.label,'整图')+' · 官方原图', 'image-status'),zoom);
      node.append(image,actions);
    } else node.append(figurePlaceholder(paper, figure));
    return node;
  }
  function openImageViewer(paper,figure){
    const asset=figure.reuseStatus==='allowed'&&safeAsset(figure.asset);if(!asset)return;
    previousImageFocus=document.activeElement;
    $('image-viewer-title').textContent=str(figure.label || paper.figure)+' · 作者论文原图';
    $('image-viewer-stage').classList.remove('native-size');
    const image=el('img');image.src=asset;image.alt=str(figure.label)+' · '+str(paper.paper);$('image-viewer-stage').replaceChildren(image);
    $('image-viewer-caption').textContent='完整官方整图，未自动裁剪。原选分面与代码的精确对应仍以核查记录为准。许可：'+str(figure.license)+'。 '+str(figure.caption,'');
    $('image-viewer-source').replaceChildren(link('查看官方出处',figure.sourceURL));
    $('image-size-toggle').textContent='切换到 100% 原始尺寸';$('image-size-toggle').setAttribute('aria-pressed','false');
    $('image-viewer').showModal();$('close-image-viewer').focus();
  }
  $('close-image-viewer').addEventListener('click',()=> $('image-viewer').close());
  $('image-viewer').addEventListener('close',()=>{if(previousImageFocus?.isConnected)previousImageFocus.focus();});
  $('image-size-toggle').addEventListener('click',()=>{const native=$('image-viewer-stage').classList.toggle('native-size');$('image-size-toggle').textContent=native?'切换到适应窗口':'切换到 100% 原始尺寸';$('image-size-toggle').setAttribute('aria-pressed',String(native));});
  function openButton(paper, className) {
    const button = el('button', '查看输入与代码 →', className); button.type = 'button'; button.addEventListener('click', () => openDetail(paper)); return button;
  }
  function renderCard(paper) {
    const card = el('article', null, 'case-card');
    card.append(figurePreview(paper, cardFigure(paper)));
    const body = el('div', null, 'card-body');
    const kicker = el('div', null, 'card-kicker'); kicker.append(el('span', str(paper.journal) + ' · ' + str(paper.year)), pill(paper)); body.append(kicker);
    heading(body, str(paper.name || paper.ai?.title_zh || paper.paper), 3);
    paragraph(body, str(paper.paper), 'paper-title'); body.append(tagGroup(paper));
    paragraph(body, paper.ai?.summary_zh || paper.description || '精确图号、绘图输入与终稿对应正在核查。', 'card-summary');
    if (aiStateLabel(paper)) paragraph(body,aiStateLabel(paper),'small');
    body.append(el('div', metricLabel(paper), 'metrics-line'));
    const bottom = el('div', null, 'card-bottom'); bottom.append(el('span', str(paper.lang, '语言待核') + ' · 未运行'), openButton(paper)); body.append(bottom);
    card.append(body); return card;
  }
  function baseMatches(paper) {
    const status = statusOf(paper);
    if(status === 'excluded' && state.status !== 'excluded')return false;
    if (state.status && !(state.status === 'pending' ? ['seed','pending'].includes(status) : status === state.status)) return false;
    if (state.search) {
      const haystack = [paper.name,paper.paper,paper.doi,paper.topic,paper.journal,paper.lang,paper.ai?.title_zh].map(value => str(value,'')).join(' ').toLocaleLowerCase();
      if (!haystack.includes(state.search.toLocaleLowerCase())) return false;
    }
    return true;
  }
  function refreshAxis(axis) {
    const form = axis === 'forms', selected = form ? state.form : state.function;
    const otherSelected = form ? state.function : state.form;
    const counts = facetCounts(catalog.papers,axis,otherSelected,baseMatches);
    const select = $(form ? 'form-filter' : 'function-filter');
    select.replaceChildren(el('option', (form ? '全部图形' : '全部功能') + '（' + counts.total + '）')); select.options[0].value = '';
    const definitions = list(catalog.dimensions?.[axis]?.all || catalog.dimensions?.[axis]?.active);
    for (const definition of definitions) {
      const count = counts.counts.get(definition.id) || 0;
      if (!count && definition.id !== selected) continue;
      const option = el('option', str(definition.label || definition.id) + '（' + count + '）'); option.value = definition.id; select.append(option);
    }
    const pendingCount = counts.pending;
    if (pendingCount || selected === '__pending') { const option = el('option', '分类待核（' + pendingCount + '）'); option.value = '__pending'; select.append(option); }
    select.value = selected;
  }
  function sortedPapers() {
    const result = catalog.papers.filter(paper => baseMatches(paper) && matchAxis(paper, 'forms', state.form) && matchAxis(paper, 'functions', state.function));
    const priorities = {ready:0,partial:1,pending:2,seed:3,error:4,excluded:5};
    result.sort((a,b) => {
      if (state.sort === 'citations') { const delta = (citationCount(b) ?? -1) - (citationCount(a) ?? -1); if (delta) return delta; }
      if (state.sort === 'evidence') { const delta = priorities[statusOf(a)] - priorities[statusOf(b)]; if (delta) return delta; }
      return str(b.date,'').localeCompare(str(a.date,'')) || str(a.id,'').localeCompare(str(b.id,''));
    });
    return result;
  }
  function renderPaperRow(paper) {
    const row = el('article', null, 'paper-row'); row.append(el('div', str(paper.year), 'year'));
    const title = el('div'); heading(title, str(paper.paper), 3); paragraph(title, str(paper.journal) + ' · DOI ' + str(paper.doi)); title.append(pill(paper)); row.append(title);
    const metrics = el('div', null, 'paper-metrics'); paragraph(metrics, metricLabel(paper)); const count = citationCount(paper); paragraph(metrics, count === null ? '引用数未获取' : 'OpenAlex 引用 ' + count); row.append(metrics, openButton(paper,'button')); return row;
  }
  function renderUpdates() {
    const node = el('div', null, 'updates-list');
    if (!reports.length) { node.append(el('div','还没有自动运行记录。人工种子案例不等于自动流水线已完成。','empty-state')); return node; }
    const reportTime=report=>str(report.finishedAt || report.startedAt || report.date || report.generatedAt || report.timestamp,'');
    for (const report of reports.slice().sort((a,b)=>reportTime(b).localeCompare(reportTime(a))).slice(0,60)) {
      const card = el('article', null, 'update-card'); heading(card, str(report.finishedAt || report.startedAt || report.date || report.generatedAt || report.timestamp, '本地核查记录'), 3);
      const summary = report.summary;
      if (typeof summary === 'string') paragraph(card, summary);
      else {
        const counts = report.counts || report;
        paragraph(card, '新收录：' + str(counts.new ?? counts.newCases ?? counts.added ?? report.newCount, '未报告') + ' · 既有修补：' + str(counts.repaired ?? counts.updated ?? report.repairCount, '未报告') + ' · 状态：' + statusLabel(report.status));
      }
      if (report.demo || report.synthetic || report.mode === 'demo') paragraph(card, '模拟演示：该记录仅用于离线功能验证，不代表真实文献抓取。', 'callout');
      items(card, list(report.errors).map(errorLabel));items(card,report.warnings);
      if(report.checkedCount !== undefined)paragraph(card,'检查 '+str(report.checkedCount)+' 条 · 新文献发现：'+statusLabel(report.discoveryStatus),'small');
      for(const line of updateBudgetLines(report))paragraph(card,line,'small');
      if(list(report.failures).length)items(card,report.failures.map(failureLabel));
      if(list(report.missingKeys).length)paragraph(card,'尚未配置：'+report.missingKeys.join(' · ')+'。这里只显示配置名，不显示密钥值。','callout');
      if(report.stageCounts){const fold=el('details');fold.append(el('summary','查看各阶段状态统计'));for(const [stage,counts]of Object.entries(report.stageCounts)){heading(fold,stageLabel(stage),4);addSpec(fold,Object.entries(counts).filter(([,count])=>counter(count)!==null).map(([status,count])=>[statusLabel(status),count]));}card.append(fold);}
      const entries = report.records || report.results || report.processed;
      if (Array.isArray(entries)) items(card, entries.map(entry => typeof entry === 'object' ? str(entry.name || entry.doi || entry.id) + ' · ' + statusLabel(entry.status) : entry));
      if (report.note) paragraph(card, report.note);
      node.append(card);
    }
    return node;
  }
  function render() {
    if (!catalog) return;
    for (const button of document.querySelectorAll('[data-view]')) button.setAttribute('aria-current', button.dataset.view === view ? 'page' : 'false');
    $('view-title').textContent = {gallery:'绘图案例',papers:'文献索引',updates:'更新日志'}[view];
    $('filters').hidden = view === 'updates';
    if (view === 'updates') { $('result-count').textContent = reports.length + ' 次运行记录'; $('content').replaceChildren(renderUpdates()); return; }
    refreshAxis('forms'); refreshAxis('functions');
    const papers = sortedPapers(); $('result-count').textContent = '显示 ' + papers.length + ' / ' + catalog.papers.length + ' 个案例';
    if (!papers.length) { $('content').replaceChildren(el('div','没有同时符合当前图形与功能的案例。可放宽条件，或选择“分类待核”。','empty-state')); return; }
    const content = el('div', null, view === 'gallery' ? 'gallery' : 'paper-list');
    for (const paper of papers) content.append(view === 'gallery' ? renderCard(paper) : renderPaperRow(paper));
    $('content').replaceChildren(content);
  }
  function addSpec(parent, fields) {
    const dl = el('dl', null, 'spec-grid');
    for (const [label,value] of fields) { dl.append(el('dt', label), el('dd', str(value))); }
    parent.append(dl);
  }
  function fieldRows(file) {
    if (Array.isArray(file.fields)) return file.fields.map(field => Array.isArray(field) ? field : [field.name || field.field,field.type || '未知',field.evidence || '待核',field.meaning || field.description || '待核']);
    if (Array.isArray(file.columns)) return file.columns.map(field => typeof field === 'string' ? [field,'未知','实际表头 / 作者代码','单位与含义待核'] : [field.name,field.type || '未知',field.evidence || '待核',field.meaning || '待核']);
    return [];
  }
  function renderInputFiles(parent, files) {
    for (const file of files) {
      const section = el('section', null, 'input-file'); heading(section, str(file.name || file.path, '精确输入路径待核'), 4);
      addSpec(section,[['用途',file.role || '直接绘图关系待核'],['格式',file.format],['方向',file.orientation],['编码',file.encoding || '未核'],['尺度 / 单位',file.scale || '未核'],['可得性',file.note || file.status || file.availability]]);
      const fields = fieldRows(file);
      if (fields.length) {
        const scroll = el('div', null, 'table-scroll'), table = el('table', null, 'fields-table');
        const head = el('thead'), tr = el('tr'); for (const text of ['字段 / 对象键','类型','依据','含义 / 限制']) tr.append(el('th', text)); head.append(tr); table.append(head);
        const body = el('tbody'); for (const field of fields) { const row = el('tr'); for (let i=0;i<4;i++) row.append(el('td',str(field[i]))); body.append(row); } table.append(body); scroll.append(table); section.append(scroll);
      } else paragraph(section,'真实表头 / 对象字段待核，不提供虚构示例数据行。');
      if (file.csv) { paragraph(section,file.preview || '字段摘录，不是完整输入文件。','small'); section.append(el('pre',file.csv)); }
      if (file.mapping) paragraph(section,'图号 / 输入对应依据：' + file.mapping,'evidence-box');
      if (file.evidence) items(section,Array.isArray(file.evidence) ? file.evidence : [file.evidence]);
      if (list(file.observedFieldReferences).length) {
        heading(section,'代码中观察到的字段引用（不等于真实表头）',4);
        items(section,file.observedFieldReferences.map(reference=>str(reference.name)+' · 代码行 '+str(reference.line)+'；对象、单位与选图对应仍待核'));
      }
      items(section,file.checks);
      if (file.url) section.append(link('核对作者输入 / 读取来源',file.url));
      parent.append(section);
    }
  }
  function renderInputs(paper, panel) {
    const spec = catalog.inputSpecs?.[paper.id] || {}, sections = inputSections(spec,paper.repository?.inputs);
    heading(panel,'原代码的输入文档'); paragraph(panel, spec.status || '直接绘图输入尚未完整核实。公开原始数据入口不等于当前图的输入已经齐全。', 'callout');
    if (spec.tree) panel.append(el('pre', spec.tree));
    else if (sections.selected.length) panel.append(el('pre', sections.selected.map(file => str(file.name || file.path, '精确路径待核')).join('\n')));
    if (spec.shape) paragraph(panel, '矩阵 / 表格方向：' + spec.shape);
    if (!sections.selected.length) paragraph(panel,'尚未观察到真实文件表头或对象字段；不从图注猜造数据列。请沿作者脚本入口定位读取步骤。');
    renderInputFiles(panel,sections.selected);
    if (sections.automatic.length) {
      const fold=el('details',null,'candidate-inputs');
      fold.append(el('summary','全仓库静态输入候选（'+sections.automatic.length+' 个 · 与选图输入分开）'));
      paragraph(fold,spec.automaticInputs.status || '这是对本轮扫描脚本的输入观察，不证明它们均用于当前选图。保留上方已核选图笔记，不相互覆盖。','callout');
      if(spec.automaticInputs.tree)fold.append(el('pre',spec.automaticInputs.tree));
      renderInputFiles(fold,sections.automatic);panel.append(fold);
    }
    paragraph(panel,'安全边界：不执行脚本，不安装作者依赖，不反序列化 RDS / pickle，不下载整套原始测序数据。','small');
  }
  function renderExplanation(paper, panel) {
    heading(panel,'中文解读'); const ai = paper.ai || {};
    if (ai.status === 'stale' && ai.summary_zh) {
      paragraph(panel,aiStateLabel(paper)+'。保留的旧说明不能作为本轮证据的解读。','callout');
      const fold=el('details');fold.append(el('summary','查看保留的旧注释（不代表本轮生成成功）'));paragraph(fold,ai.summary_zh);items(fold,ai.learning_points);panel.append(fold);
      paragraph(panel,'最近重生成状态：'+diagnosticLabel(ai)+' · 旧生成时间：'+str(ai.generated_at),'small');
    }
    else if (ai.summary_zh && ['ok','success','generated','complete','completed','ready'].includes(ai.status)) { paragraph(panel,ai.summary_zh); heading(panel,'值得学习的步骤',4); items(panel,ai.learning_points); paragraph(panel,ai.reviewRequired || ai.review_required ? 'AI 学习注释待核：仅依据已收集的文本证据，不证明代码已运行或生成了原图。' : 'AI 解读仅依据已收集的文本证据，不证明代码已运行或生成了原图。','callout'); }
    else { paragraph(panel,'AI 中文解读尚未完成：' + diagnosticLabel(ai) + '。缺失时保留来源核查，不补造解读。','callout'); if (paper.description) { heading(panel,'人工种子概述（不是本轮 AI 结果）',4); paragraph(panel,paper.description); } }
    if (list(ai.evidence_ids).length) paragraph(panel,'证据标识：' + ai.evidence_ids.join(' · '),'small');
    if (list(ai.limitations).length) { heading(panel,'解读限制',4); items(panel,ai.limitations); }
    heading(panel,'期刊与引用注释',4); paragraph(panel,metricLabel(paper));
    addSpec(panel,[['指标来源',paper.metrics?.source || '未获取'],['获取时间',paper.metrics?.retrieved_at || '未报告'],['中科院分区',paper.metrics?.cas || '未获取']]);
    if(paper.metrics?.last_attempt_status)paragraph(panel,'本轮指标获取：'+diagnosticLabel(paper.metrics)+'；当前显示的历史指标不代表本轮匹配成功。','callout');
    else if(paper.metrics?.status && !['matched','ok','success'].includes(paper.metrics.status))paragraph(panel,'期刊指标状态：'+diagnosticLabel(paper.metrics),'callout');
    if(paper.metrics?.persistent_cache_hit)paragraph(panel,'本轮使用持久化指标缓存，不是重新查询。','small');
    if(paper.metrics?.retained_field_asof) {heading(panel,'沿用字段的原获取时间',4);addSpec(panel,Object.entries(paper.metrics.retained_field_asof));}
    const count = citationCount(paper); paragraph(panel,count === null ? '引用数未获取；不会按 0 次引用处理。' : 'OpenAlex 引用数：' + count + '（' + str(paper.citations?.retrievedAt || paper.citations?.retrieved_at || paper.citationMetrics?.retrieved_at,'获取时间未报告') + '）。');
    paragraph(panel,'IF、JCR、引用数只用于选文偏好。引用随年份、领域与数据库覆盖变化；不视为研究质量或复现证据。','small');
  }
  function renderSources(paper, panel) {
    heading(panel,'作者代码与静态对应'); const repository = paper.repository || {};
    paragraph(panel,repository.commit ? '已固定代码提交：' + repository.commit : '代码提交尚未固定；旧种子的 main / master 链接可能随作者更新变化。','callout');
    if(repository.retained_from_previous||repository.last_attempt_status)paragraph(panel,'本轮代码读取：'+diagnosticLabel(repository)+'；显示上次保留的静态核查，不代表本轮读取成功。','callout');
    if(validHTTP(repository.errorStatus))paragraph(panel,'代码来源异常：'+httpStatusLabel(repository.errorStatus),'callout');
    const links = el('div',null,'inline-links'); for (const [label,url] of [['论文',paper.paperURL],['作者仓库',repository.url || paper.codeURL],['种子脚本入口',paper.scriptURL],['数据声明',paper.dataURL]]) if (url) links.append(link(label,url)); panel.append(links);
    addSpec(panel,[['代码许可',repository.license || paper.codeLicense || '未核'],['图像许可',paper.article?.license || paper.figureLicense || '未核'],['对应范围',paper.dimensionLinkScope || '待核']]);
    if(paper.article?.licenseURL)panel.append(link('文章许可条款',paper.article.licenseURL));
    if(list(paper.article?.codeURLEvidence).length){heading(panel,'作者代码可用性声明依据',4);const declarationLinks=el('ul');for(const evidence of paper.article.codeURLEvidence){const li=el('li');li.append(link('声明中的作者仓库',evidence.url),el('span',' · '+str(evidence.status,'待核')+' · '),link('核对官方声明来源'+(evidence.sectionID?' / '+evidence.sectionID:''),evidence.sourceURL));declarationLinks.append(li);}panel.append(declarationLinks);}
    if (paper.evidence) paragraph(panel,paper.evidence,'evidence-box');
    for (const script of list(repository.scripts)) {
      heading(panel,str(script.path),4); panel.append(link('固定版本源代码',script.url));
      addSpec(panel,[['语言',script.language],['SHA-256',script.sha256],['行号范围',script.lineScope === 'notebook code-cell concatenation' ? 'Notebook 代码单元拼接后的行号，不是原 JSON 文件行号' : script.lineScope || '源文件']]);
      if(list(script.evidence).length){const references=el('ul');for(const evidence of script.evidence){const item=el('li');if(typeof evidence==='object'){item.append(link('静态来源行 '+str(evidence.line,'未报告')+' · '+str(evidence.type,'引用'),evidence.url || script.url));if(evidence.type==='limitation'&&evidence.text)item.append(el('span',' · '+evidence.text));}else item.textContent=str(evidence);references.append(item);}panel.append(references);}
      if (list(script.inputs).length) paragraph(panel,'读取入口：' + script.inputs.map(value => typeof value === 'string' ? value : str(value.path || value.name)).join(' · '));
    }
    if (list(repository.figureMappings).length) { heading(panel,'图号映射证据',4); items(panel,repository.figureMappings.map(mapping => typeof mapping === 'string' ? mapping : str(mapping.figureLabel || mapping.figure || mapping.label || mapping.path) + ' · ' + str(mapping.output,'') + ' · 读取/输出行 '+str(mapping.line)+' · ' + str(mapping.evidence || mapping.status))); }
    heading(panel,'可追溯来源',4); const sources = el('ul'); for (const source of list(paper.sources)) { const li=el('li'); li.append(link(str(source.label,'来源'),source.url)); sources.append(li); } panel.append(sources);
    if (paper.article?.xmlURL) panel.append(link('官方全文 XML',paper.article.xmlURL));
    if (list(paper.article?.supplements).length) {
      heading(panel,'补充材料清单（不发布文件副本）',4);
      const ul=el('ul'); for(const item of paper.article.supplements){const li=el('li');li.append(link(str(item.label || item.name || item.id,'补充材料'),item.sourceURL || item.url));li.append(el('span',' · ' + str(item.status || item.reuseStatus,'待核')));ul.append(li);}panel.append(ul);
    }
    paragraph(panel,'这里只给作者代码链接与来源核查笔记；代码许可未知时不再发布注释版脚本。论文“免费可读”也不等于图像可复制、改图或裁剪。','small');
  }
  function renderNotes(paper, panel) {
    heading(panel,'核查笔记'); paragraph(panel,'未执行作者代码。未验证生成结果与论文原图一致。','callout');
    if (paper.notes) panel.append(el('pre',paper.notes));
    heading(panel,'尚待确认',4); items(panel,[...list(paper.limitations),...list(paper.repository?.limitations),...list(paper.article?.limitations)],'仍需人工核对完整环境、图号与终稿版本。');
    if (paper.automation?.errors?.length) { heading(panel,'本轮异常',4); items(panel,paper.automation.errors.map(errorLabel)); }
    if (paper.automation?.warnings?.length) { heading(panel,'构建 / 核查提醒',4); items(panel,paper.automation.warnings); }
    if(list(paper.suggestedGraphForms).length||list(paper.suggestedAnalysisFunctions).length){
      const fold=el('details');fold.append(el('summary','自动分类建议（未批准，不进入筛选目录）'));
      paragraph(fold,'以下是文字 / 函数规则提出的候选，不是已验证图形或逐分面的功能对应。','callout');
      for(const [axis,suggestions,title]of [['forms',paper.suggestedGraphForms,'图形形态建议'],['functions',paper.suggestedAnalysisFunctions,'分析功能建议']]){heading(fold,title,4);items(fold,list(suggestions).map(suggestion=>{const id=typeof suggestion==='object'?suggestion.id:suggestion;return str(typeof suggestion==='object'?suggestion.label:'',labels[axis].get(id)||id)+' · 待核';}),'暂未提出建议');}
      panel.append(fold);
    }
    if(paper.selectionScore){const fold=el('details');fold.append(el('summary','自动选文偏好评分（不是研究质量评分）'));paragraph(fold,paper.selectionScore.meaning);addSpec(fold,[['合计',paper.selectionScore.total],...Object.entries(paper.selectionScore.components||{})]);panel.append(fold);}
    if (paper.automation?.stages) { heading(panel,'流水线阶段',4); addSpec(panel,Object.entries(paper.automation.stages).map(([name,value])=>[stageLabel(name),diagnosticLabel(value)])); }
    paragraph(panel,'最后核查：' + str(paper.automation?.lastCheckedAt || paper.checkedAt),'small');
    paragraph(panel,paper.dimensionLinkEvidence || '分类连接同一个选图记录；复合图中逐分面“图形—功能—脚本”的关系尚未自动验证。','small');
  }
  function renderDetailPanel() {
    const panel = $('detail-panel'); panel.replaceChildren();
    for (const button of document.querySelectorAll('[data-tab]')) { const selected = button.dataset.tab === activeTab; button.setAttribute('aria-selected',String(selected)); button.tabIndex = selected ? 0 : -1; }
    panel.setAttribute('aria-labelledby','tab-' + activeTab);
    ({inputs:renderInputs,explanation:renderExplanation,source:renderSources,notes:renderNotes}[activeTab])(activePaper,panel);
  }
  function openDetail(paper) {
    previousFocus=document.activeElement; activePaper=paper; activeTab='inputs'; $('detail-title').textContent=str(paper.name || paper.ai?.title_zh || paper.paper);
    const meta=$('detail-meta');meta.replaceChildren();meta.className='detail-meta';paragraph(meta,str(paper.paper));paragraph(meta,str(paper.journal)+' · '+str(paper.date)+' · DOI '+str(paper.doi));meta.append(pill(paper),tagGroup(paper));
    const figurePanel=$('detail-figure');figurePanel.replaceChildren();const figures=safeFigures(paper);
    if (!figures.length) {
      figurePanel.append(figurePreview(paper,list(paper.article?.figures)[0]));paragraph(figurePanel,'此处不使用示意图代替原图。原图许可、出处或下载尚未满足展示条件。');
      if (paper.figureLicense || paper.article?.license) paragraph(figurePanel,'图像许可：'+str(paper.article?.license || paper.figureLicense));
    }
    for (const figure of figures.slice(0,3)) {
      const item=el('div',null,'figure-item');item.append(figurePreview(paper,figure),link('查看官方原图',figure.sourceURL));
      if(figure.caption)paragraph(item,figure.caption,'caption');paragraph(item,'许可：'+str(figure.license)+' · '+str(figure.attribution,'出处待核'));
      if(figure.licenseURL)item.append(link('阅读许可条款',figure.licenseURL));
      paragraph(item,'尺寸：'+str(figure.width)+' × '+str(figure.height)+' px；未宣称已复现。','small');figurePanel.append(item);
    }
    renderDetailPanel(); $('detail').showModal(); $('close-detail').focus();
  }
  $('close-detail').addEventListener('click',()=> $('detail').close());
  $('detail').addEventListener('close',()=> { if (previousFocus?.isConnected) previousFocus.focus(); });
  $('detail').addEventListener('click',event=> { if(event.target===$('detail')) { const box=$('detail').getBoundingClientRect();if(event.clientX<box.left||event.clientX>box.right||event.clientY<box.top||event.clientY>box.bottom)$('detail').close(); } });
  const tabButtons=[...document.querySelectorAll('[data-tab]')];
  tabButtons.forEach((button,index)=> {
    button.addEventListener('click',()=> {activeTab=button.dataset.tab;renderDetailPanel();});
    button.addEventListener('keydown',event=> {let target=null;if(event.key==='ArrowRight')target=tabButtons[(index+1)%tabButtons.length];if(event.key==='ArrowLeft')target=tabButtons[(index+tabButtons.length-1)%tabButtons.length];if(event.key==='Home')target=tabButtons[0];if(event.key==='End')target=tabButtons.at(-1);if(target){event.preventDefault();activeTab=target.dataset.tab;renderDetailPanel();target.focus();}});
  });
  document.querySelectorAll('[data-view]').forEach(button=>button.addEventListener('click',()=>{view=button.dataset.view;history.replaceState(null,'','#'+view);render();}));
  window.addEventListener('hashchange',()=>{view=viewFromHash(location.hash,view);render();});
  for (const [id,key] of [['form-filter','form'],['function-filter','function'],['status-filter','status'],['sort','sort']]) $(id).addEventListener('change',event=>{state[key]=event.target.value;render();});
  $('search').addEventListener('input',event=>{state.search=event.target.value.trim();render();});
  $('reset').addEventListener('click',()=>{Object.assign(state,{form:'',function:'',status:'',search:'',sort:'evidence'});$('search').value='';$('status-filter').value='';$('sort').value='evidence';render();});
  Promise.all([fetch('data/catalog.json',{cache:'no-store'}).then(response=>{if(!response.ok)throw Error('catalog HTTP '+response.status);return response.json();}),fetch('data/updates.json',{cache:'no-store'}).then(response=>response.ok?response.json():[]).catch(()=>[])])
    .then(([data,updates])=>{
      if(!Array.isArray(data.papers)||data.authorCodeExecuted!==false)throw Error('目录结构不符合静态核查约定');
      catalog=data;reports=Array.isArray(updates)?updates:list(updates.reports);
      for(const axis of ['forms','functions'])for(const definition of list(data.dimensions?.[axis]?.all || data.dimensions?.[axis]?.active))labels[axis].set(definition.id,definition.label || definition.id);
      const ready=data.papers.filter(paper=>statusOf(paper)==='ready').length;
      $('collection-summary').textContent=data.papers.length+' 个案例 · '+ready+' 个静态核查就绪';
      $('notice').textContent=(data.synthetic?'离线模拟目录：记录不是实际文献。 ':'')+'每次最多收录 5 篇新文献；失败或证据不足的记录保留待核。所有案例均未执行作者代码，未验证原图复现。';
      $('generated-at').textContent='目录构建：'+str(data.generatedAt || data.checkedAt,'未报告');
      view=viewFromHash(location.hash,view);
      render();
    }).catch(error=>{$('notice').textContent='目录无法读取：'+error.message+'。请通过本地 HTTP 预览服务打开，不要直接双击 HTML。';$('notice').classList.add('error');$('content').replaceChildren(el('div','网站界面已就绪，但数据加载失败。请检查构建与预览命令。','empty-state'));});
})();
