import { PageHeader } from '../components/Page.jsx';
import { Empty } from '../components/State.jsx';

export default function Logs() {
  return (
    <div>
      <PageHeader
        title="Logs"
        description="Request traces, sessions, and API access for one agent."
      />
      <Empty>Requests / Sessions / API access tabs land in Phase 2.</Empty>
    </div>
  );
}
