import { Navigate, Route, Routes } from 'react-router-dom';
import Shell from './components/Shell.jsx';
import Overview from './routes/Overview.jsx';
import Agents from './routes/Agents.jsx';
import Hosts from './routes/Hosts.jsx';
import Logs from './routes/Logs.jsx';
import Feedback from './routes/Feedback.jsx';

export default function App() {
  return (
    <Routes>
      <Route element={<Shell />}>
        <Route index element={<Overview />} />
        <Route path="agents" element={<Agents />} />
        <Route path="hosts" element={<Hosts />} />
        <Route path="logs" element={<Logs />} />
        <Route path="feedback" element={<Feedback />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
