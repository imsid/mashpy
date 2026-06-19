import { PageHeader } from '../components/Page.jsx';
import { Empty } from '../components/State.jsx';

export default function Hosts() {
  return (
    <div>
      <PageHeader
        title="Hosts"
        description="Active compositions: a primary agent, its subagents, and workflows."
      />
      <Empty>Host composition view lands in Phase 2.</Empty>
    </div>
  );
}
