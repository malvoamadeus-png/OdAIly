import { useEffect, useMemo, useState, type FormEvent } from 'react';
import { ChevronLeft, ChevronRight, RotateCcw, Search } from 'lucide-react';
import { getSystemInformationDetail, getSystemInformationRegistry, type SystemInformationDetail, type SystemInformationRegistry } from './xCaptureStore';
import './systemInformation.css';

type Props = { instanceId?: string | null; onOpenInformation?: () => void };

export default function WorkflowGuidePanel({ instanceId, onOpenInformation }: Props) {
  const [mode, setMode] = useState<'example' | 'actual'>(instanceId ? 'actual' : 'example');
  const [registry, setRegistry] = useState<SystemInformationRegistry | null>(null);
  const [flowKey, setFlowKey] = useState('x');
  const [scenarioKey, setScenarioKey] = useState('regular');
  const [step, setStep] = useState(0);
  const [recordId, setRecordId] = useState(instanceId || '');
  const [actual, setActual] = useState<SystemInformationDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const flow = useMemo(() => registry?.registry.find((item) => item.key === flowKey) || registry?.registry[0], [flowKey, registry]);
  const scenario = useMemo(() => flow?.scenarios.find((item) => item.key === scenarioKey) || flow?.scenarios[0], [flow, scenarioKey]);

  useEffect(() => {
    void getSystemInformationRegistry().then((value) => {
      setRegistry(value);
      setFlowKey((current) => value.registry.some((item) => item.key === current) ? current : value.registry[0]?.key || 'x');
    }).catch((err: Error) => setError(err.message));
  }, []);

  useEffect(() => {
    if (!instanceId) return;
    setMode('actual');
    setRecordId(instanceId);
    void loadActual(instanceId);
  }, [instanceId]);

  useEffect(() => {
    if (!flow?.scenarios.length) return;
    if (!flow.scenarios.some((item) => item.key === scenarioKey)) setScenarioKey(flow.scenarios[0].key);
    setStep(0);
  }, [flowKey]);

  async function loadActual(id = recordId) {
    if (!id.trim()) return;
    setLoading(true);
    setError('');
    try {
      const detail = await getSystemInformationDetail(id.trim());
      setActual(detail);
      setFlowKey(detail.business_key);
    } catch (err) {
      setActual(null);
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }

  function submitActual(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void loadActual();
  }

  return (
    <section className="workflowGuide">
      <div className="workflowGuideHeader">
        <div className="workflowGuideTabs" role="tablist" aria-label="流程查看方式">
          <button type="button" role="tab" aria-selected={mode === 'example'} className={mode === 'example' ? 'active' : ''} onClick={() => setMode('example')}>跟着例子走</button>
          <button type="button" role="tab" aria-selected={mode === 'actual'} className={mode === 'actual' ? 'active' : ''} onClick={() => setMode('actual')}>查看实际路径</button>
        </div>
      </div>

      {mode === 'example' ? (
        <div className="workflowGuideExample">
          <div className="workflowGuideSelectors">
            <label><span>业务入口</span><select value={flow?.key || flowKey} onChange={(event) => setFlowKey(event.target.value)}>
              {(registry?.registry || []).map((item) => <option key={item.key} value={item.key}>{item.label}</option>)}
            </select></label>
            <label><span>处理场景</span><select value={scenario?.key || ''} onChange={(event) => { setScenarioKey(event.target.value); setStep(0); }}>
              {(flow?.scenarios || []).map((item) => <option key={item.key} value={item.key}>{item.label}</option>)}
            </select></label>
            <span className="workflowGuideEntry">{flow?.entry}</span>
          </div>
          {scenario && <>
            <div className="workflowGuideStep">
              <span>第 {step + 1} 步 / {scenario.steps.length}</span>
              <strong>{scenario.steps[step]}</strong>
              <p>{step === scenario.steps.length - 1 ? '此场景在这里结束。' : `下一步：${scenario.steps[step + 1]}`}</p>
            </div>
            <div className="workflowGuideProgress" aria-label={`第${step + 1}步，共${scenario.steps.length}步`}>
              {scenario.steps.map((label, index) => <span key={`${index}-${label}`} className={index < step ? 'done' : index === step ? 'current' : ''} title={label} />)}
            </div>
            <div className="workflowGuideActions">
              <button className="secondaryButton" type="button" disabled={step === 0} onClick={() => setStep((value) => Math.max(0, value - 1))}><ChevronLeft size={16} /> 上一步</button>
              <button className="iconButton" type="button" title="重新开始" onClick={() => setStep(0)}><RotateCcw size={16} /></button>
              <button className="secondaryButton" type="button" disabled={step >= scenario.steps.length - 1} onClick={() => setStep((value) => Math.min(scenario.steps.length - 1, value + 1))}>下一步 <ChevronRight size={16} /></button>
            </div>
          </>}
        </div>
      ) : (
        <div className="workflowGuideActual">
          <form className="workflowGuideLookup" onSubmit={submitActual}>
            <label><span>信息流记录 ID</span><input value={recordId} onChange={(event) => setRecordId(event.target.value)} placeholder="例如 task:x:12345" /></label>
            <button className="primaryButton" type="submit" disabled={loading}><Search size={16} /> 查看路径</button>
            {onOpenInformation && <button className="secondaryButton" type="button" onClick={onOpenInformation}>打开全量信息流</button>}
          </form>
          {error && <div className="notice error" role="alert">{error}</div>}
          {loading ? <div className="emptyState">正在读取记录…</div> : actual ? <>
            <div className="workflowGuideRecord"><strong>{actual.title || actual.source_id}</strong><span>{actual.business_key} · {actual.source} · {actual.result}</span></div>
            {Boolean(actual.legacy_history) && <p className="systemInformationNotice">旧记录只显示已保存状态。未保存的历史步骤保持未知，不按流程目录补全。</p>}
            <ol className="workflowGuideActualList">
              {actual.timeline.map((event) => <li key={event.event_id}>
                <span className="workflowGuideActualMarker" />
                <time>{formatTime(event.happened_at_ms || event.observed_at_ms)}</time>
                <strong>{event.stage} · {event.summary}</strong>
                {event.reason && <p>{event.reason}</p>}
              </li>)}
            </ol>
            <div className="workflowGuideUnknown">没有保存的步骤显示为未知；重试、等待和降级只按实际记录呈现。</div>
          </> : <div className="emptyState">输入全量信息流中的记录 ID。</div>}
        </div>
      )}
    </section>
  );
}

function formatTime(value: number): string {
  return new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(value);
}
