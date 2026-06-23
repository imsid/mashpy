import { Navigate, Route, Routes } from 'react-router-dom';
import Shell from './components/Shell.jsx';
import Overview from './routes/Overview.jsx';
import Agents from './routes/Agents.jsx';
import Tools from './routes/Tools.jsx';
import ToolDetail from './routes/ToolDetail.jsx';
import Skills from './routes/Skills.jsx';
import SkillDetail from './routes/SkillDetail.jsx';
import Workflows from './routes/Workflows.jsx';
import Hosts from './routes/Hosts.jsx';
import Logs from './routes/Logs.jsx';
import Feedback from './routes/Feedback.jsx';
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
        <Route path="hosts" element={<Hosts />} />
        <Route path="logs" element={<Logs />} />
        <Route path="feedback" element={<Feedback />} />
        <Route path="reference" element={<Reference />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
