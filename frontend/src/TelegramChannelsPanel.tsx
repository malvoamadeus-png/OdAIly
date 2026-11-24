import { useEffect, useState } from 'react';
import { Save } from 'lucide-react';
import { listTelegramChannelNames, updateTelegramChannelName, type TelegramChannelName } from './xCaptureStore';

const labels: Record<string, string> = { vida: '投机之路', durov: 'Durov' };

export default function TelegramChannelsPanel() {
  const [channels, setChannels] = useState<TelegramChannelName[]>([]);
  const [message, setMessage] = useState('');
  useEffect(() => {
    listTelegramChannelNames().then(setChannels).catch((error) => setMessage(String(error)));
  }, []);
  async function save(channel: TelegramChannelName) {
    try {
      await updateTelegramChannelName(channel.channel_key, channel.write_name.trim());
      setMessage('写作名已保存');
    } catch (error) {
      setMessage(String(error));
    }
  }
  return <section className="section">
    <div className="sectionHeader"><h2>Telegram</h2></div>
    {message && <div className="notice">{message}</div>}
    <div className="accountList">
      {channels.map((channel) => <div className="accountRow" key={channel.channel_key}>
        <strong>{labels[channel.channel_key] || channel.channel_key}</strong>
        <input aria-label={`${labels[channel.channel_key] || channel.channel_key}写作名`} value={channel.write_name} onChange={(event) => setChannels((rows) => rows.map((row) => row.channel_key === channel.channel_key ? { ...row, write_name: event.target.value } : row))} />
        <button className="iconButton" type="button" title="保存写作名" aria-label="保存写作名" disabled={!channel.write_name.trim()} onClick={() => void save(channel)}><Save size={17} /></button>
      </div>)}
    </div>
  </section>;
}
