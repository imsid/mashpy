import { PageHeader } from '../components/Page.jsx';
import { Empty } from '../components/State.jsx';

export default function Agents() {
  return (
    <div>
      <PageHeader
        title="Agents"
        description="The role-less agent pool — the building blocks for hosts."
      />
      <Empty>Agent catalog lands in Phase 2.</Empty>
    </div>
  );
}
