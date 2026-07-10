import { Navigate, Route, Routes } from 'react-router-dom';
import Shell from './components/Shell.jsx';
import Overview from './routes/Overview.jsx';
import Agents from './routes/Agents.jsx';
import Tools from './routes/Tools.jsx';
import ToolDetail from './routes/ToolDetail.jsx';
import Skills from './routes/Skills.jsx';
import SkillDetail from './routes/SkillDetail.jsx';
import Workflows from './routes/Workflows.jsx';
import WorkflowDetail from './routes/WorkflowDetail.jsx';
import WorkflowRuns from './routes/WorkflowRuns.jsx';
import WorkflowRunDetail from './routes/WorkflowRunDetail.jsx';
import Hosts from './routes/Hosts.jsx';
import Logs from './routes/Logs.jsx';
import Feedback from './routes/Feedback.jsx';
import Evals from './routes/Evals.jsx';
import EvalDetail from './routes/EvalDetail.jsx';
import ExperimentDetail from './routes/ExperimentDetail.jsx';
import ExperimentCompare from './routes/ExperimentCompare.jsx';
import Reference from './routes/Reference.jsx';

export default function App() {
  return (
    <Routes>
      <Route element={<Shell />}>
        <Route index element={<Overview />} />
        <Route path="agents" element={<Agents />} />
        <Route path="tools" element={<Tools />} />
        <Route path="tools/:toolName" element={<ToolDetail />} />
        <Route path="skills" element={<Skills />} />
        <Route path="skills/:skillName" element={<SkillDetail />} />
        <Route path="workflows" element={<Workflows />} />
        <Route path="workflows/:workflowId" element={<WorkflowDetail />} />
        <Route path="workflows/:workflowId/runs" element={<WorkflowRuns />} />
        <Route path="workflows/:workflowId/runs/:runId" element={<WorkflowRunDetail />} />
        <Route path="hosts" element={<Hosts />} />
        <Route path="logs" element={<Logs />} />
        <Route path="feedback" element={<Feedback />} />
        <Route path="evals" element={<Evals />} />
        <Route path="evals/:evalId" element={<EvalDetail />} />
        <Route path="evals/:evalId/compare" element={<ExperimentCompare />} />
        <Route path="evals/:evalId/experiments/:experimentId" element={<ExperimentDetail />} />
        <Route path="reference" element={<Reference />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
