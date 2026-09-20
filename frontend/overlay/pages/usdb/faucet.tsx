import type { NextPage } from 'next';
import dynamic from 'next/dynamic';
import React from 'react';

import PageNextJs from 'nextjs/PageNextJs';

const Faucet = dynamic(() => import('ui/usdb/Faucet'), { ssr: false });
const Page: NextPage = () => <PageNextJs pathname="/usdb/faucet"><Faucet/></PageNextJs>;

export default Page;
export { base as getServerSideProps } from 'nextjs/getServerSideProps/main';
