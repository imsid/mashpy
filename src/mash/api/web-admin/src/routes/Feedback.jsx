import { PageHeader } from '../components/Page.jsx';
import { Empty } from '../components/State.jsx';

export default function Feedback() {
  return (
    <div>
      <PageHeader
        title="Feedback"
        description="Notes captured via the REPL /feedback command."
      />
      <Empty>Feedback browser lands in Phase 2.</Empty>
    </div>
  );
}
