import { chakra } from '@chakra-ui/react';
import React from 'react';

interface Props {
  isCollapsed?: boolean;
  onClick?: (event: React.SyntheticEvent) => void;
  className?: string;
}

/** Use the bundled mark unchanged in both themes and keep the compact sidebar readable. */
export default function Brand({ isCollapsed, onClick, className }: Props) {
  return (
    <chakra.a
      className={ className }
      href="/"
      onClick={ onClick }
      aria-label="USDB Explorer home"
      data-testid="usdb-brand"
      display="inline-flex"
      alignItems="center"
      gap="10px"
      flexShrink={ 0 }
      width={{ base: '120px', lg: isCollapsed === false ? '120px' : '30px', xl: isCollapsed ? '30px' : '120px' }}
      height="30px"
      overflow="hidden"
      color={{ base: '#242846', _dark: '#f4f4ff' }}
    >
      <chakra.img src="/assets/usdb/logo.png" alt="USDB logo" width="30px" height="30px" flexShrink={ 0 } borderRadius="50%"/>
      <chakra.span
        fontSize="23px"
        fontWeight="700"
        letterSpacing="-0.5px"
        lineHeight="1"
        display={{ base: 'block', lg: isCollapsed === false ? 'block' : 'none', xl: isCollapsed ? 'none' : 'block' }}
      >USDB</chakra.span>
    </chakra.a>
  );
}
